package main

import (
	"bufio"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ---------------------------------------------------------------- GPU encoder detection
type encoder struct{ id, name string }

var (
	gpuMu     sync.Mutex
	gpuEnc    *encoder // nil = none found (or not checked yet)
	gpuDone   bool
	gpuBroken = map[string]bool{}
	gpuList   = hwEncoders()
)

// hwEncoders: the hardware H.264 encoders worth trying on this OS, best first.
// A Mac only has Apple's VideoToolbox; NVENC/QSV/AMF are Windows/Linux cards.
func hwEncoders() []encoder {
	if runtime.GOOS == "darwin" {
		return []encoder{{"h264_videotoolbox", "Apple"}}
	}
	return []encoder{{"h264_nvenc", "NVIDIA"}, {"h264_qsv", "Intel"}, {"h264_amf", "AMD"}}
}

// detectGPU tries a tiny 4K test encode with each hardware encoder and keeps the first that works.
func detectGPU() {
	for _, e := range gpuList {
		args := []string{"-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=black:s=3840x2160:r=30:d=1"}
		// test with the real settings (middle quality), so an encoder that cannot
		// take them - e.g. constant-quality VideoToolbox on an Intel Mac - is skipped
		args = append(args, encArgs(e.id, "balanced")...)
		args = append(args, "-f", "null", "-")
		c := ffCmd(args...)
		done := make(chan error, 1)
		if err := c.Start(); err != nil {
			continue
		}
		go func() { done <- c.Wait() }()
		select {
		case err := <-done:
			if err == nil {
				gpuMu.Lock()
				ee := e
				gpuEnc, gpuDone = &ee, true
				gpuMu.Unlock()
				fmt.Println("  Fast GPU encoding: ON (" + e.name + ")")
				return
			}
		case <-time.After(20 * time.Second):
			c.Process.Kill()
		}
	}
	gpuMu.Lock()
	gpuDone = true
	gpuMu.Unlock()
	fmt.Println("  No GPU encoder found - using the CPU (fast settings).")
}

func gpuName() string {
	gpuMu.Lock()
	defer gpuMu.Unlock()
	if !gpuDone {
		return "checking"
	}
	if gpuEnc == nil || gpuBroken[gpuEnc.id] {
		return ""
	}
	return gpuEnc.name
}

func pickEncoder(c Config) string {
	if c.Encoder == "cpu" {
		return "libx264"
	}
	for i := 0; i < 120; i++ { // wait for the GPU check to finish (max ~60 s)
		gpuMu.Lock()
		d := gpuDone
		gpuMu.Unlock()
		if d {
			break
		}
		time.Sleep(500 * time.Millisecond)
	}
	gpuMu.Lock()
	defer gpuMu.Unlock()
	if gpuEnc != nil && !gpuBroken[gpuEnc.id] {
		return gpuEnc.id
	}
	return "libx264"
}

// encArgs: video encoder settings per quality level (high / balanced / fast).
func encArgs(enc, q string) []string {
	lvl := map[string]int{"high": 0, "balanced": 1, "fast": 2}[q]
	switch enc {
	case "h264_nvenc":
		return []string{"-c:v", "h264_nvenc", "-preset", []string{"p5", "p4", "p2"}[lvl], "-rc", "vbr",
			"-cq", []string{"19", "22", "25"}[lvl], "-b:v", "0", "-pix_fmt", "yuv420p"}
	case "h264_qsv":
		return []string{"-c:v", "h264_qsv", "-preset", []string{"slow", "medium", "veryfast"}[lvl],
			"-global_quality", []string{"20", "23", "26"}[lvl], "-pix_fmt", "nv12"}
	case "h264_videotoolbox":
		// -q:v is VideoToolbox constant quality (1-100, higher = better); Apple silicon only.
		return []string{"-c:v", "h264_videotoolbox", "-q:v", []string{"68", "60", "52"}[lvl],
			"-profile:v", "high", "-pix_fmt", "yuv420p"}
	case "h264_amf":
		qp := []string{"19", "22", "25"}[lvl]
		return []string{"-c:v", "h264_amf", "-quality", []string{"quality", "balanced", "speed"}[lvl],
			"-rc", "cqp", "-qp_i", qp, "-qp_p", qp, "-qp_b", qp, "-pix_fmt", "yuv420p"}
	}
	return []string{"-c:v", "libx264", "-preset", []string{"faster", "veryfast", "ultrafast"}[lvl],
		"-crf", []string{"19", "21", "23"}[lvl], "-pix_fmt", "yuv420p"}
}

// ---------------------------------------------------------------- building the ffmpeg command
type clip struct {
	path          string
	info          Info
	start, length float64 // part of the file to use (intro/outro inside rhymes cut away)
}

func buildArgs(clips []clip, c Config, enc, out string) []string {
	var W, H int
	fmt.Sscanf(c.Resolution, "%dx%d", &W, &H)
	if W == 0 || H == 0 {
		W, H = 1920, 1080
	}
	fps, _ := strconv.ParseFloat(c.Fps, 64)
	fade := c.Transition == "fade"
	F := c.FadeSeconds
	if F <= 0 {
		F = 0.5
	}
	args := []string{"-hide_banner", "-y"}
	for _, cl := range clips {
		if cl.start > 0 {
			args = append(args, "-ss", fmt.Sprintf("%.3f", cl.start))
		}
		args = append(args, "-t", fmt.Sprintf("%.3f", cl.length), "-i", cl.path)
	}
	n := len(clips)
	silent := map[int]int{}
	for i, cl := range clips {
		if !cl.info.HasAudio {
			silent[i] = n + len(silent)
			args = append(args, "-f", "lavfi", "-t", fmt.Sprintf("%.3f", cl.length), "-i", "anullsrc=r=48000:cl=stereo")
		}
	}
	var parts []string
	concat := ""
	for i, cl := range clips {
		d := cl.length
		v := fmt.Sprintf("[%d:v:0]", i)
		var vf []string
		// skip resizing when the clip already has the right size (saves a lot of time at 4K)
		if cl.info.Width != W || cl.info.Height != H {
			vf = append(vf, fmt.Sprintf("scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black", W, H, W, H))
		}
		vf = append(vf, "setsar=1")
		if cl.info.Fps == 0 || math.Abs(cl.info.Fps-fps) > 0.01 {
			vf = append(vf, "fps="+c.Fps)
		}
		vf = append(vf, "format=yuv420p")
		asrc := fmt.Sprintf("[%d:a:0]", i)
		if s, ok := silent[i]; ok {
			asrc = fmt.Sprintf("[%d:a]", s)
		}
		a := asrc + "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
		if fade && d > 3*F {
			st := d - F
			vf = append(vf, fmt.Sprintf("fade=t=in:st=0:d=%g,fade=t=out:st=%.3f:d=%g", F, st, F))
			a += fmt.Sprintf(",afade=t=in:st=0:d=%g,afade=t=out:st=%.3f:d=%g", F, st, F)
		}
		vf = append(vf, fmt.Sprintf("trim=duration=%.3f,setpts=PTS-STARTPTS", d))
		a += fmt.Sprintf(",atrim=duration=%.3f,asetpts=PTS-STARTPTS", d)
		parts = append(parts, fmt.Sprintf("%s%s[v%d]", v, strings.Join(vf, ","), i), fmt.Sprintf("%s[a%d]", a, i))
		concat += fmt.Sprintf("[v%d][a%d]", i, i)
	}
	parts = append(parts, fmt.Sprintf("%sconcat=n=%d:v=1:a=1[vout][aout]", concat, n))
	args = append(args, "-filter_complex", strings.Join(parts, ";"), "-map", "[vout]", "-map", "[aout]")
	args = append(args, encArgs(enc, c.Quality)...)
	args = append(args, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
		"-progress", "pipe:1", "-nostats", out)
	return args
}

// ---------------------------------------------------------------- queue
type mergeOpts struct {
	Resolution  string  `json:"resolution"`
	Fps         string  `json:"fps"`
	Transition  string  `json:"transition"`
	FadeSeconds float64 `json:"fade_seconds"`
	Quality     string  `json:"quality"`
	CutInner    string  `json:"cut_inner"`
	Encoder     string  `json:"encoder"`
	Name        string  `json:"name"`
	Lang        string  `json:"lang"`
}

type QJob struct {
	ID   int       `json:"id"`
	Refs []string  `json:"refs"`
	Opts mergeOpts `json:"opts"`
	// queued -> running (encoding) -> filing -> filed; or error | cancelled | not_on_nas.
	// not_on_nas: encoded, but the copy to the NAS failed; the local file is kept
	// and "File again" (/api/queue/refile) retries only the copy.
	State        string  `json:"state"`
	Progress     float64 `json:"progress"`      // encoding, 0..1
	FileProgress float64 `json:"file_progress"` // copying to the NAS, 0..1
	Total        float64 `json:"total"`
	Name         string  `json:"name"`        // display name
	Output       string  `json:"output"`      // ref for /video?ref=... ("job:<id>") once there is a file
	OutputPath   string  `json:"output_path"` // where the comp is filed on the NAS
	LocalPath    string  `json:"local_path"`  // the encoded file on this Mac, until swept
	Filed        float64 `json:"filed"`       // when it landed on the NAS (unix seconds)
	Encoder      string  `json:"encoder"`
	Message      string  `json:"message"`
	Clips        int     `json:"clips"`
	Added        float64 `json:"added"`
	Started      float64 `json:"started"`
	Elapsed      float64 `json:"elapsed"`
	Speed        float64 `json:"speed"`    // x realtime
	Chapters     string  `json:"chapters"` // YouTube timestamps
	cmd          *exec.Cmd
	cancel       bool
}

var (
	qMu      sync.Mutex
	queue    []*QJob
	nextID   = 1
	reserved = map[string]bool{} // output paths taken by running jobs
	badChars = regexp.MustCompile(`[<>:"/\\|?*]`)
)

func queuePath() string { return filepath.Join(cacheDir, "queue.json") }

// saveQueue must be called with qMu held.
func saveQueue() {
	b, _ := json.MarshalIndent(queue, "", " ")
	os.WriteFile(queuePath(), b, 0644)
}

// loadQueue restores the queue after a restart: unfinished comps are queued again,
// and a comp interrupted while filing waits for "File again".
func loadQueue() {
	b, err := os.ReadFile(queuePath())
	if err != nil {
		return
	}
	var jobs []*QJob
	if json.Unmarshal(b, &jobs) != nil {
		return
	}
	for _, j := range jobs {
		switch j.State {
		case "running":
			removeLocal(j)
			j.State, j.Progress, j.Message, j.OutputPath, j.Output = "queued", 0, "Restarted after the engine stopped", "", ""
		case "filing":
			if isFile(j.LocalPath) {
				j.State, j.Message = "not_on_nas", "Interrupted while filing to the NAS"
			} else {
				j.State, j.Message, j.Output = "error", "Interrupted while filing, and the local copy is gone", ""
			}
		}
		if j.ID >= nextID {
			nextID = j.ID + 1
		}
	}
	queue = jobs
	go schedule()
}

// schedule starts queued jobs while fewer than "Run together" are running.
func schedule() {
	qMu.Lock()
	defer qMu.Unlock()
	limit := loadConfig().Parallel
	if limit < 1 {
		limit = 1
	}
	running := 0
	for _, j := range queue {
		if j.State == "running" {
			running++
		}
	}
	for _, j := range queue {
		if running >= limit {
			break
		}
		if j.State == "queued" {
			j.State, j.Message, j.Started = "running", "Starting…", float64(time.Now().UnixMilli())/1000
			running++
			go runJob(j)
		}
	}
	saveQueue()
}

func update(j *QJob, f func()) {
	qMu.Lock()
	f()
	qMu.Unlock()
}

// removeLocal deletes a job's work folder (the encoded file before/after filing).
func removeLocal(j *QJob) {
	if j.LocalPath != "" {
		if d := filepath.Dir(j.LocalPath); within(workDir, d) && d != workDir {
			os.RemoveAll(d)
		}
	}
	j.LocalPath = ""
}

func finish(j *QJob, state, msg string) {
	qMu.Lock()
	j.State, j.Message, j.cmd = state, msg, nil
	delete(reserved, strings.ToLower(j.OutputPath))
	switch state {
	case "error", "cancelled":
		removeLocal(j)
		j.OutputPath, j.Output = "", ""
	case "filed":
		j.Progress, j.FileProgress = 1, 1
		j.Filed = float64(time.Now().UnixMilli()) / 1000
		recordUsage(j.Refs)
	}
	if j.Started > 0 && j.Elapsed == 0 {
		j.Elapsed = float64(time.Now().UnixMilli())/1000 - j.Started
	}
	saveQueue()
	qMu.Unlock()
	go schedule()
}

// shareOK: the NAS that comps are filed to is mounted (/Volumes/DATA for an output
// folder under it). Outside /Volumes, the output folder's parent must exist. Comps
// are refused, and filing waits, while it is not.
func shareOK() bool {
	c := loadConfig()
	p := cleanPath(c.OutputFolder)
	if p == "" {
		return isDir(cleanPath(c.RhymesFolder))
	}
	if root, ok := shareRoot(p); ok {
		return isDir(root)
	}
	return isDir(filepath.Dir(p))
}

const shareDown = "The NAS isn't reachable - is the share mounted on the Mac mini?"

// fileJob copies the encoded comp to the NAS (output folder/<Language>/), next to
// its "<name> - Timestamps.txt". It writes to "<name>.mp4.part" and renames, so a
// half-copied comp never appears under its real name.
func fileJob(j *QJob) {
	update(j, func() { j.State, j.Message, j.FileProgress = "filing", "Filing to the NAS…", 0 })
	fail := func(msg string) { finish(j, "not_on_nas", msg) }
	if !shareOK() {
		fail(shareDown)
		return
	}
	src, err := os.Open(j.LocalPath)
	if err != nil {
		finish(j, "error", "The encoded file is gone - queue the comp again")
		return
	}
	defer src.Close()
	st, _ := src.Stat()

	// The name was reserved when encoding started; someone may have filed a comp
	// under it since, so it is checked again now.
	qMu.Lock()
	dest := j.OutputPath
	if isFile(dest) {
		delete(reserved, strings.ToLower(dest))
		dir, base := filepath.Dir(dest), strings.TrimSuffix(filepath.Base(dest), ".mp4")
		for k := 2; isFile(dest) || reserved[strings.ToLower(dest)]; k++ {
			dest = filepath.Join(dir, fmt.Sprintf("%s (%d).mp4", base, k))
		}
		reserved[strings.ToLower(dest)] = true
		j.OutputPath, j.Name = dest, filepath.Base(dest)
	}
	qMu.Unlock()

	if err := os.MkdirAll(filepath.Dir(dest), 0755); err != nil {
		fail("Could not create the folder on the NAS: " + err.Error())
		return
	}
	part := dest + ".part"
	out, err := os.Create(part)
	if err != nil {
		fail("Could not write to the NAS: " + err.Error())
		return
	}
	total := float64(st.Size())
	n, err := io.Copy(out, &progressReader{r: src, fn: func(done int64) {
		update(j, func() { j.FileProgress = math.Min(0.999, float64(done)/math.Max(1, total)) })
	}})
	if cerr := out.Close(); err == nil {
		err = cerr
	}
	if err == nil && n != st.Size() {
		err = fmt.Errorf("copied %d of %d bytes", n, st.Size())
	}
	if err == nil {
		if fi, e := os.Stat(part); e != nil || fi.Size() != st.Size() {
			err = fmt.Errorf("size check failed after copying")
		}
	}
	if err == nil {
		err = os.Rename(part, dest)
	}
	if err != nil {
		os.Remove(part)
		fail("Copy to the NAS failed: " + err.Error())
		return
	}
	if j.Chapters != "" {
		body := j.Chapters
		if runtime.GOOS == "windows" { // Notepad wants CRLF; macOS/Linux editors don't
			body = strings.ReplaceAll(body, "\n", "\r\n")
		}
		txt := strings.TrimSuffix(dest, filepath.Ext(dest)) + " - Timestamps.txt"
		os.WriteFile(txt, []byte(body), 0644)
	}
	finish(j, "filed", "On the NAS")
}

type progressReader struct {
	r    io.Reader
	n    int64
	last int64
	fn   func(int64)
}

func (p *progressReader) Read(b []byte) (int, error) {
	k, err := p.r.Read(b)
	p.n += int64(k)
	if p.n-p.last >= 8<<20 {
		p.last = p.n
		p.fn(p.n)
	}
	return k, err
}

func runJob(j *QJob) {
	c := loadConfig()
	o := j.Opts
	for _, kv := range []struct {
		v   string
		dst *string
	}{{o.Resolution, &c.Resolution}, {o.Fps, &c.Fps}, {o.Transition, &c.Transition}, {o.Quality, &c.Quality}, {o.CutInner, &c.CutInner}, {o.Encoder, &c.Encoder}} {
		if kv.v != "" {
			*kv.dst = kv.v
		}
	}
	if o.FadeSeconds > 0 {
		c.FadeSeconds = o.FadeSeconds
	}
	update(j, func() { j.Message = "Reading clips…" })

	var clips []clip
	total := 0.0
	for _, r := range j.Refs {
		p := resolveVideo(r)
		if p == "" {
			finish(j, "error", "File not found: "+strings.TrimPrefix(r, "rhyme:"))
			return
		}
		i := probe(p)
		cl := clip{path: p, info: i, length: i.Duration}
		if strings.HasPrefix(r, "rhyme:") {
			s, e := cutFor(p, c.CutInner)
			cl.start, cl.length = s, i.Duration-s-e
		}
		clips = append(clips, cl)
		total += cl.length
	}

	j.Chapters = chapters(j.Refs, clips, o.Lang)

	// encoded to work/<id>/<name>.mp4 on this Mac, then filed to
	// <output folder>/<Language>/<name>.mp4 on the NAS, never overwriting
	lang := strings.TrimSpace(badChars.ReplaceAllString(o.Lang, "_"))
	name := strings.TrimSpace(badChars.ReplaceAllString(o.Name, "_"))
	if name == "" {
		name = "Comp_" + time.Now().Format("2006-01-02_1504")
		if lang != "" {
			name = lang + " Rhymes " + name
		}
	}
	name = strings.TrimSuffix(name, filepath.Ext(name))
	outDir := outputFolder()
	if lang != "" {
		outDir = filepath.Join(outDir, lang)
	}
	local := filepath.Join(workDir, strconv.Itoa(j.ID), name+".mp4")
	os.RemoveAll(filepath.Dir(local))
	if err := os.MkdirAll(filepath.Dir(local), 0755); err != nil {
		finish(j, "error", "Could not create the work folder: "+err.Error())
		return
	}
	qMu.Lock()
	outPath := filepath.Join(outDir, name+".mp4")
	for k := 2; isFile(outPath) || reserved[strings.ToLower(outPath)]; k++ {
		outPath = filepath.Join(outDir, fmt.Sprintf("%s (%d).mp4", name, k))
	}
	reserved[strings.ToLower(outPath)] = true
	j.OutputPath, j.LocalPath, j.Total = outPath, local, total
	j.Name = filepath.Base(outPath)
	qMu.Unlock()

	if j.cancel {
		finish(j, "cancelled", "Cancelled")
		return
	}
	update(j, func() { j.Message = "Preparing encoder…" })
	enc := pickEncoder(c)
	for attempt := 0; attempt < 2; attempt++ {
		err := encode(j, clips, c, enc)
		if j.cancel {
			finish(j, "cancelled", "Cancelled")
			return
		}
		if err == nil {
			update(j, func() { j.Output = fmt.Sprintf("job:%d", j.ID) })
			fileJob(j)
			return
		}
		if enc != "libx264" { // GPU failed: remember and retry on the CPU
			gpuMu.Lock()
			gpuBroken[enc] = true
			gpuMu.Unlock()
			enc = "libx264"
			update(j, func() { j.Progress, j.Message = 0, "GPU failed, retrying on CPU…" })
			continue
		}
		finish(j, "error", err.Error())
		return
	}
}

func encode(j *QJob, clips []clip, c Config, enc string) error {
	args := buildArgs(clips, c, enc, j.LocalPath)
	logPath := filepath.Join(cacheDir, fmt.Sprintf("merge_log_%d.txt", j.ID))
	logF, _ := os.Create(logPath)
	fmt.Fprintf(logF, "%s %s\n\n", ffmpegExe(), strings.Join(args, " "))
	cmd := ffCmd(args...)
	cmd.Stderr = logF
	stdout, _ := cmd.StdoutPipe()
	label := map[string]string{"h264_nvenc": "GPU (NVIDIA)", "h264_qsv": "GPU (Intel)", "h264_amf": "GPU (AMD)", "h264_videotoolbox": "GPU (Apple)"}[enc]
	if label == "" {
		label = "CPU"
	}
	update(j, func() { j.cmd, j.Encoder, j.Message = cmd, label, "Merging…" })
	start := time.Now()
	if j.cancel {
		logF.Close()
		return fmt.Errorf("cancelled")
	}
	if err := cmd.Start(); err != nil {
		logF.Close()
		return fmt.Errorf("could not start ffmpeg: %v", err)
	}
	sc := bufio.NewScanner(stdout)
	for sc.Scan() {
		line := sc.Text()
		if strings.HasPrefix(line, "out_time_us=") {
			if us, err := strconv.ParseFloat(line[12:], 64); err == nil && j.Total > 0 {
				sec := us / 1e6
				update(j, func() {
					j.Progress = math.Max(0, math.Min(0.999, sec/j.Total))
					if el := time.Since(start).Seconds(); el > 1 {
						j.Speed = sec / el
					}
				})
			}
		}
	}
	err := cmd.Wait()
	logF.Close()
	if err == nil {
		os.Remove(logPath)
		return nil
	}
	b, _ := os.ReadFile(logPath)
	s := string(b)
	if len(s) > 1500 {
		s = s[len(s)-1500:]
	}
	return fmt.Errorf("ffmpeg failed (full log: %s):\n%s", logPath, s)
}

// ---------------------------------------------------------------- http
func queueRoutes(m *http.ServeMux) {
	m.HandleFunc("/api/queue", func(w http.ResponseWriter, r *http.Request) {
		qMu.Lock()
		out := make([]QJob, 0, len(queue))
		for _, j := range queue {
			out = append(out, *j)
		}
		qMu.Unlock()
		writeJSON(w, map[string]any{"jobs": out, "gpu": gpuName(), "parallel": loadConfig().Parallel}, 200)
	})
	m.HandleFunc("/api/queue/add", func(w http.ResponseWriter, r *http.Request) {
		var in struct {
			Refs    []string  `json:"refs"`
			Options mergeOpts `json:"options"`
		}
		json.NewDecoder(r.Body).Decode(&in)
		if len(in.Refs) == 0 {
			writeJSON(w, map[string]any{"error": "Nothing selected"}, 400)
			return
		}
		if ffmpegExe() == "" {
			writeJSON(w, map[string]any{"error": ffmpegMissing()}, 400)
			return
		}
		if !shareOK() {
			writeJSON(w, map[string]any{"error": shareDown}, 400)
			return
		}
		qMu.Lock()
		j := &QJob{ID: nextID, Refs: in.Refs, Opts: in.Options, State: "queued", Message: "Waiting…",
			Added: float64(time.Now().UnixMilli()) / 1000}
		j.Name = strings.TrimSpace(in.Options.Name)
		if j.Name == "" {
			j.Name = strings.TrimSpace(in.Options.Lang + " Rhymes Comp")
		}
		for _, ref := range in.Refs {
			if strings.HasPrefix(ref, "rhyme:") {
				j.Clips++
			}
		}
		nextID++
		queue = append(queue, j)
		saveQueue()
		qMu.Unlock()
		go schedule()
		writeJSON(w, map[string]any{"ok": true, "id": j.ID}, 200)
	})
	m.HandleFunc("/api/queue/cancel", func(w http.ResponseWriter, r *http.Request) {
		var in struct {
			ID int `json:"id"`
		}
		json.NewDecoder(r.Body).Decode(&in)
		qMu.Lock()
		for _, j := range queue {
			if j.ID != in.ID {
				continue
			}
			if j.State == "queued" {
				j.State, j.Message = "cancelled", "Cancelled"
			} else if j.State == "running" {
				j.cancel = true
				if j.cmd != nil && j.cmd.Process != nil {
					j.cmd.Process.Kill()
				}
			}
		}
		saveQueue()
		qMu.Unlock()
		writeJSON(w, map[string]any{"ok": true}, 200)
	})
	m.HandleFunc("/api/queue/move", func(w http.ResponseWriter, r *http.Request) { // move a waiting job up/down
		var in struct{ ID, Dir int }
		json.NewDecoder(r.Body).Decode(&in)
		qMu.Lock()
		for i, j := range queue {
			if j.ID == in.ID && j.State == "queued" {
				k := i + in.Dir
				for k >= 0 && k < len(queue) && queue[k].State != "queued" {
					k += in.Dir
				}
				if k >= 0 && k < len(queue) {
					queue[i], queue[k] = queue[k], queue[i]
				}
				break
			}
		}
		saveQueue()
		qMu.Unlock()
		writeJSON(w, map[string]any{"ok": true}, 200)
	})
	m.HandleFunc("/api/queue/clear", func(w http.ResponseWriter, r *http.Request) { // remove finished jobs from the list
		qMu.Lock()
		keep := queue[:0]
		for _, j := range queue {
			switch j.State {
			case "queued", "running", "filing", "not_on_nas": // not_on_nas: its only copy is local
				keep = append(keep, j)
			case "filed":
				removeLocal(j)
			}
		}
		queue = keep
		saveQueue()
		qMu.Unlock()
		writeJSON(w, map[string]any{"ok": true}, 200)
	})
	m.HandleFunc("/api/queue/refile", func(w http.ResponseWriter, r *http.Request) { // "File again"
		var in struct {
			ID int `json:"id"`
		}
		json.NewDecoder(r.Body).Decode(&in)
		qMu.Lock()
		var job *QJob
		for _, j := range queue {
			if j.ID == in.ID && j.State == "not_on_nas" {
				job = j
			}
		}
		if job == nil {
			qMu.Unlock()
			writeJSON(w, map[string]any{"error": "That comp is not waiting to be filed"}, 409)
			return
		}
		if !isFile(job.LocalPath) {
			job.State, job.Message, job.Output = "error", "The local copy is gone - queue the comp again", ""
			saveQueue()
			qMu.Unlock()
			writeJSON(w, map[string]any{"error": job.Message}, 410)
			return
		}
		job.State = "filing" // claimed: a second click finds it no longer not_on_nas
		qMu.Unlock()
		go fileJob(job)
		writeJSON(w, map[string]any{"ok": true}, 200)
	})
	m.HandleFunc("/api/queue/parallel", func(w http.ResponseWriter, r *http.Request) {
		var in struct {
			N int `json:"n"`
		}
		json.NewDecoder(r.Body).Decode(&in)
		if in.N >= 1 && in.N <= 4 {
			c := loadConfig()
			c.Parallel = in.N
			saveConfig(c)
		}
		go schedule()
		writeJSON(w, map[string]any{"ok": true}, 200)
	})
}

// ---------------------------------------------------------------- YouTube timestamps
var reNoise = regexp.MustCompile(`(?i)\b(final|mp4|mov|hd|4k|1080p|v\d+)\b`)
var reSpaces = regexp.MustCompile(`[\s_]+`)
var reEmptyBrackets = regexp.MustCompile(`\(\s*\)|\[\s*\]`)

// cleanTitle turns "Aaye Re Aaye Tiye_Hindi" / "Aakash Gire - Hindi" into "Aaye Re Aaye Tiye" / "Aakash Gire".
func cleanTitle(file, lang string) string {
	t := strings.TrimSuffix(filepath.Base(file), filepath.Ext(file))
	t = reSpaces.ReplaceAllString(t, " ")
	if lang != "" {
		t = regexp.MustCompile(`(?i)\b`+regexp.QuoteMeta(lang)+`\b`).ReplaceAllString(t, " ")
	}
	t = reNoise.ReplaceAllString(t, " ")
	t = reEmptyBrackets.ReplaceAllString(t, " ") // "(HD)" -> "()" -> gone; "(with intro)" stays whole
	t = reSpaces.ReplaceAllString(t, " ")
	t = strings.Trim(t, " -–_.,")
	t = regexp.MustCompile(`\s*-\s*$`).ReplaceAllString(t, "")
	if t == "" {
		t = strings.TrimSuffix(filepath.Base(file), filepath.Ext(file))
	}
	return t
}

func ytTime(sec float64, long bool) string {
	s := int(math.Floor(sec + 0.001))
	if long {
		return fmt.Sprintf("%d:%02d:%02d", s/3600, s%3600/60, s%60)
	}
	return fmt.Sprintf("%d:%02d", s/60, s%60)
}

// chapters: the first rhyme starts at 0:00 (the intro is folded into it, because YouTube
// needs the first chapter at 0:00 and each chapter to be at least 10 seconds).
func chapters(refs []string, clips []clip, lang string) string {
	total := 0.0
	for _, c := range clips {
		total += c.length
	}
	long := total >= 3600
	var lines []string
	t := 0.0
	first := true
	for i, r := range refs {
		if strings.HasPrefix(r, "rhyme:") {
			at := t
			if first {
				at, first = 0, false
			}
			rl := lang
			if rl == "" { // mixed languages: drop each rhyme's own folder name from its title
				if k := strings.Index(r[6:], "/"); k > 0 {
					rl = r[6 : 6+k]
				}
			}
			lines = append(lines, ytTime(at, long)+" "+cleanTitle(r[6:], rl))
		}
		t += clips[i].length
	}
	return strings.Join(lines, "\n")
}
