// Comp Creator engine - merges rhyme videos into one compilation with an intro
// at the start and an outro at the end, then files it to the NAS.
// Headless: it serves a JSON API on 127.0.0.1:8765 that Content Hub proxies
// (api/routers/comps.py); the operator's UI is Content Hub's Compilations pane.
package main

import (
	"crypto/md5"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"runtime"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const defaultAddr = "127.0.0.1:8765"
const appVersion = "7"

var (
	appDir, cfgPath, cacheDir, thumbDir, infoPath, workDir string
	videoExts                                              = map[string]bool{".mp4": true, ".mov": true, ".mkv": true, ".avi": true, ".webm": true, ".m4v": true, ".wmv": true, ".flv": true, ".mpg": true, ".mpeg": true}
)

// ---------------------------------------------------------------- config
type Config struct {
	RhymesFolder string  `json:"rhymes_folder"`
	Intro        string  `json:"intro"`
	Outro        string  `json:"outro"`
	IOFolder     string  `json:"intro_outro_folder"`
	OutputFolder string  `json:"output_folder"`
	FfmpegPath   string  `json:"ffmpeg_path"`
	Resolution   string  `json:"resolution"`
	Fps          string  `json:"fps"`
	Transition   string  `json:"transition"`
	FadeSeconds  float64 `json:"fade_seconds"`
	Quality      string  `json:"quality"`
	CutInner     string  `json:"cut_inner"`
	Encoder      string  `json:"encoder"`  // auto (GPU if available) | cpu
	Parallel     int     `json:"parallel"` // comps encoded at the same time
	// hours an encoded comp stays on this Mac after it is filed to the NAS
	LocalRetentionHours float64 `json:"local_retention_hours"`
}

var cfgMu sync.Mutex

func defaults() Config {
	return Config{Resolution: "1920x1080", Fps: "30", Transition: "fade", FadeSeconds: 0.5, Quality: "high", CutInner: "auto", Encoder: "auto", Parallel: 2, LocalRetentionHours: 48}
}

func loadConfig() Config {
	cfgMu.Lock()
	defer cfgMu.Unlock()
	c := defaults()
	if b, err := os.ReadFile(cfgPath); err == nil {
		json.Unmarshal(b, &c)
	}
	return c
}

func saveConfig(c Config) error {
	cfgMu.Lock()
	defer cfgMu.Unlock()
	b, _ := json.MarshalIndent(c, "", "  ")
	return os.WriteFile(cfgPath, b, 0644)
}

func cleanPath(p string) string {
	p = strings.Trim(strings.TrimSpace(p), `"'`)
	if p == "" {
		return ""
	}
	if strings.HasPrefix(p, "~") {
		if h, err := os.UserHomeDir(); err == nil {
			p = filepath.Join(h, p[1:])
		}
	}
	return filepath.Clean(p)
}

func isDir(p string) bool  { st, err := os.Stat(p); return p != "" && err == nil && st.IsDir() }
func isFile(p string) bool { st, err := os.Stat(p); return p != "" && err == nil && !st.IsDir() }

// ---------------------------------------------------------------- ffmpeg
func ffmpegExe() string {
	c := loadConfig()
	if isFile(c.FfmpegPath) {
		return c.FfmpegPath
	}
	name := "ffmpeg"
	if runtime.GOOS == "windows" {
		name = "ffmpeg.exe"
	}
	for _, p := range []string{filepath.Join(appDir, name), filepath.Join(appDir, "ffmpeg", name), filepath.Join(appDir, "bin", name)} {
		if isFile(p) {
			return p
		}
	}
	if p, err := exec.LookPath("ffmpeg"); err == nil {
		return p
	}
	// A launchd service gets a bare PATH (/usr/bin:/bin:...), so Homebrew's ffmpeg
	// is not found by LookPath there. Check its usual homes directly.
	if runtime.GOOS == "darwin" {
		for _, p := range []string{"/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"} {
			if isFile(p) {
				return p
			}
		}
	}
	return ""
}

func ffmpegMissing() string {
	if runtime.GOOS == "windows" {
		return "ffmpeg.exe not found - keep it in the same folder as CompCreator.exe"
	}
	return "ffmpeg not found - install it (brew install ffmpeg) or set ffmpeg_path in config.json"
}

func ffCmd(args ...string) *exec.Cmd {
	c := exec.Command(ffmpegExe(), args...)
	hide(c)
	return c
}

type Info struct {
	Duration float64 `json:"duration"`
	Width    int     `json:"width"`
	Height   int     `json:"height"`
	HasAudio bool    `json:"has_audio"`
	Fps      float64 `json:"fps"`
}

var (
	infoMu    sync.Mutex
	infoCache = map[string]Info{}
	reDur     = regexp.MustCompile(`Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)`)
	reVid     = regexp.MustCompile(`Stream #.*?Video:.*?(\d{2,5})x(\d{2,5})`)
	reAud     = regexp.MustCompile(`Stream #.*?Audio:`)
	reFps     = regexp.MustCompile(`Stream #.*?Video:.*?([\d.]+) fps`)
)

func fileKey(p string) string {
	st, err := os.Stat(p)
	if err != nil {
		return ""
	}
	abs, _ := filepath.Abs(p)
	h := md5.Sum([]byte(fmt.Sprintf("%s|%d|%d", abs, st.Size(), st.ModTime().Unix())))
	return hex.EncodeToString(h[:])
}

func probe(p string) Info {
	key := fileKey(p)
	infoMu.Lock()
	if i, ok := infoCache[key]; ok {
		infoMu.Unlock()
		return i
	}
	infoMu.Unlock()
	out, _ := ffCmd("-hide_banner", "-i", p).CombinedOutput()
	s := string(out)
	var i Info
	if m := reDur.FindStringSubmatch(s); m != nil {
		h, _ := strconv.Atoi(m[1])
		mi, _ := strconv.Atoi(m[2])
		se, _ := strconv.ParseFloat(m[3], 64)
		i.Duration = float64(h*3600+mi*60) + se
	}
	if m := reVid.FindStringSubmatch(s); m != nil {
		i.Width, _ = strconv.Atoi(m[1])
		i.Height, _ = strconv.Atoi(m[2])
	}
	i.HasAudio = reAud.MatchString(s)
	if m := reFps.FindStringSubmatch(s); m != nil {
		i.Fps, _ = strconv.ParseFloat(m[1], 64)
	}
	infoMu.Lock()
	infoCache[key] = i
	b, _ := json.Marshal(infoCache)
	infoMu.Unlock()
	os.WriteFile(infoPath, b, 0644)
	return i
}

var thumbSem = make(chan struct{}, 3)

func thumbnail(p string) string {
	out := filepath.Join(thumbDir, fileKey(p)+".jpg")
	if isFile(out) {
		return out
	}
	thumbSem <- struct{}{}
	defer func() { <-thumbSem }()
	if isFile(out) {
		return out
	}
	d := probe(p).Duration
	if d == 0 {
		d = 3
	}
	ss := d * 0.4 // middle of the rhyme, past any built-in intro
	ffCmd("-hide_banner", "-loglevel", "error", "-y", "-ss", fmt.Sprintf("%.2f", ss), "-i", p,
		"-frames:v", "1", "-vf", "scale=400:-2", "-q:v", "4", out).Run()
	if isFile(out) {
		return out
	}
	return ""
}

// ---------------------------------------------------------------- library
type Rhyme struct {
	Rel    string  `json:"rel"`
	Name   string  `json:"name"`
	Folder string  `json:"folder"`
	Lang   string  `json:"lang"`
	Size   int64   `json:"size"`
	Mtime  float64 `json:"mtime"`
	Used   int     `json:"used"`      // comps it has been filed in
	Last   float64 `json:"last_used"` // when it was last filed in one (unix seconds)
}

var reNum = regexp.MustCompile(`\d+|\D+`)

func naturalLess(a, b string) bool {
	pa, pb := reNum.FindAllString(strings.ToLower(a), -1), reNum.FindAllString(strings.ToLower(b), -1)
	for i := 0; i < len(pa) && i < len(pb); i++ {
		if pa[i] == pb[i] {
			continue
		}
		na, ea := strconv.Atoi(pa[i])
		nb, eb := strconv.Atoi(pb[i])
		if ea == nil && eb == nil && na != nb {
			return na < nb
		}
		return pa[i] < pb[i]
	}
	return len(pa) < len(pb)
}

// outputFolder is asked for once per folder while walking a library (skipDir), and
// working it out lists the library root and creates the folder - over SMB, every
// time. So the answer is kept for a little while per config.
var (
	outMu    sync.Mutex
	outCache struct {
		key, path string
		at        time.Time
	}
)

func outputFolder() string {
	c := loadConfig()
	key := c.RhymesFolder + "|" + c.OutputFolder
	outMu.Lock()
	if outCache.key == key && time.Since(outCache.at) < 30*time.Second {
		p := outCache.path
		outMu.Unlock()
		return p
	}
	outMu.Unlock()
	p := findOutputFolder(c)
	outMu.Lock()
	outCache.key, outCache.path, outCache.at = key, p, time.Now()
	outMu.Unlock()
	return p
}

func findOutputFolder(c Config) string {
	out := cleanPath(c.OutputFolder)
	if out == "" {
		rf := cleanPath(c.RhymesFolder)
		if rf == "" {
			rf = appDir
		}
		out = filepath.Join(rf, "Comps")
		// use an existing "Comps Outs" / "Comps" / "Output" folder in the library if there is one
		if ents, err := os.ReadDir(rf); err == nil {
			for _, e := range ents {
				if e.IsDir() && reCompsDir.MatchString(e.Name()) {
					out = filepath.Join(rf, e.Name())
					break
				}
			}
		}
	}
	os.MkdirAll(out, 0755)
	abs, _ := filepath.Abs(out)
	return abs
}

type Lang struct {
	Name  string `json:"name"`
	Count int    `json:"count"`
}

// languages = the top-level folders of the library (Hindi, Gujarati, Bangla...),
// except the Intro & Outro folder, comps folder and app folder.
func languages(items []Rhyme) []Lang {
	folder := cleanPath(loadConfig().RhymesFolder)
	count := map[string]int{}
	for _, it := range items {
		count[it.Lang]++
	}
	langs := []Lang{}
	if ents, err := os.ReadDir(folder); err == nil {
		for _, e := range ents {
			if !e.IsDir() || skipDir(filepath.Join(folder, e.Name()), e.Name()) ||
				reIntro.MatchString(e.Name()) || reOutro.MatchString(e.Name()) {
				continue
			}
			langs = append(langs, Lang{e.Name(), count[e.Name()]})
		}
	}
	sort.Slice(langs, func(i, j int) bool { return naturalLess(langs[i].Name, langs[j].Name) })
	if count[""] > 0 { // videos lying directly in the library folder
		langs = append(langs, Lang{"", count[""]})
	}
	return langs
}

func listRhymes() ([]Rhyme, error) {
	folder := cleanPath(loadConfig().RhymesFolder)
	if !isDir(folder) {
		return nil, errors.New("Rhymes folder not set or not found")
	}
	return scanRhymes(folder, ""), nil
}

// maxScan caps one listing, so pasting a folder far too high up the share
// returns in reasonable time instead of walking the whole NAS.
const maxScan = 3000

// scanRhymes lists the videos under folder. With lang == "" it is the configured
// library: refs are relative to it and the language is the top-level folder. With
// a lang it is a pasted folder: refs are absolute paths and every rhyme is in lang.
func scanRhymes(folder, lang string) []Rhyme {
	io := introOutro()
	items := []Rhyme{}
	walkVideos(folder, func(full, rel string) bool {
		if len(items) >= maxScan {
			return false
		}
		if relHasMatch(rel, reIntro) || relHasMatch(rel, reOutro) ||
			strings.EqualFold(full, io.Intro) || strings.EqualFold(full, io.Outro) ||
			(io.Folder != "" && within(io.Folder, full)) {
			return true // intro/outro are added automatically, never listed as rhymes
		}
		st, err := os.Stat(full)
		if err != nil {
			return true
		}
		dir := filepath.ToSlash(filepath.Dir(rel))
		if dir == "." {
			dir = ""
		}
		key, l := rel, lang
		if lang != "" {
			key = filepath.ToSlash(full)
		} else if i := strings.Index(rel, "/"); i > 0 {
			l = rel[:i]
		}
		name := filepath.Base(rel)
		u := usageOf(key)
		items = append(items, Rhyme{Rel: key, Name: strings.TrimSuffix(name, filepath.Ext(name)),
			Folder: dir, Lang: l, Size: st.Size(), Mtime: float64(st.ModTime().Unix()), Used: u.Count, Last: u.Last})
		return true
	})
	sort.Slice(items, func(i, j int) bool { return naturalLess(items[i].Rel, items[j].Rel) })
	return items
}

// ---------------------------------------------------------------- pasted folders
// mountsDir is where macOS mounts NAS shares. browseRoots are where a pasted
// folder may be: on the Mac mini, the mounted drives and shares. Empty means
// anywhere (Windows, where the engine is only ever run by hand).
var (
	mountsDir   = "/Volumes"
	browseRoots []string
	reDrive     = regexp.MustCompile(`^[A-Za-z]:[\\/]`)
	reMountDup  = regexp.MustCompile(`-\d+$`)
)

func allowed(p string) bool {
	if len(browseRoots) == 0 {
		return true
	}
	for _, r := range browseRoots {
		if within(r, p) {
			return true
		}
	}
	return false
}

// mountOf finds where a share is mounted: /Volumes/DATA, or the "DATA-1" macOS
// makes when the share was mounted twice, ignoring case.
func mountOf(share string) string {
	want := filepath.Join(mountsDir, share)
	if isDir(want) {
		return want
	}
	if ents, err := os.ReadDir(mountsDir); err == nil {
		for _, e := range ents {
			if n := reMountDup.ReplaceAllString(e.Name(), ""); strings.EqualFold(n, share) && isDir(filepath.Join(mountsDir, e.Name())) {
				return filepath.Join(mountsDir, e.Name())
			}
		}
	}
	return want
}

// localDir turns the folder someone pasted into the path on this Mac. People copy
// it from wherever they are browsing the NAS: \\10.1.1.3\DATA\Animations\Rhymes\Hindi
// (Windows), smb://10.1.1.3/DATA/Animations/Rhymes/Hindi (Finder's Connect to
// Server), or /Volumes/DATA/Animations/Rhymes/Hindi (a Mac with the share mounted).
// The server part is ignored: the share is found by its name under /Volumes.
func localDir(in string) (string, error) {
	p := strings.Trim(strings.TrimSpace(in), `"'`)
	if p == "" {
		return "", errors.New("Paste the path of a folder on the NAS")
	}
	if runtime.GOOS != "windows" {
		low := strings.ToLower(p)
		unc := ""
		switch {
		case strings.HasPrefix(low, "smb://"):
			if u, err := url.PathUnescape(p[6:]); err == nil {
				unc = u
			} else {
				unc = p[6:]
			}
		case strings.HasPrefix(p, `\\`), strings.HasPrefix(p, "//"):
			unc = p
		case reDrive.MatchString(p):
			return "", errors.New(`That's a drive letter on a Windows PC, which the Mac mini can't see. Paste the NAS path instead, like \\10.1.1.3\DATA\Animations\Rhymes\Hindi`)
		}
		if unc != "" {
			parts := strings.FieldsFunc(unc, func(r rune) bool { return r == '\\' || r == '/' })
			if len(parts) < 2 {
				return "", errors.New("That path names the NAS but no share or folder on it")
			}
			p = filepath.Join(append([]string{mountOf(parts[1])}, parts[2:]...)...)
		}
	}
	p = cleanPath(p)
	if !filepath.IsAbs(p) {
		return "", errors.New("Paste the full path of the folder, starting from the NAS")
	}
	if !allowed(p) {
		return "", errors.New("Only folders on the NAS can be used")
	}
	for _, r := range browseRoots {
		if rel, err := filepath.Rel(r, p); err == nil && !strings.Contains(rel, string(filepath.Separator)) {
			return "", errors.New("That's a whole share. Paste the folder that holds the rhyme videos")
		}
	}
	if !isDir(p) {
		if share, ok := shareRoot(p); ok && !isDir(share) {
			return "", fmt.Errorf("The %s share isn't connected on the Mac mini", filepath.Base(share))
		}
		return "", errors.New("There's no folder at that path on the NAS")
	}
	return p, nil
}

// shareRoot: /Volumes/DATA for anything under it. ok is false outside /Volumes.
func shareRoot(p string) (string, bool) {
	rel, err := filepath.Rel(mountsDir, p)
	if err != nil || rel == "." || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) {
		return "", false
	}
	return filepath.Join(mountsDir, strings.Split(rel, string(filepath.Separator))[0]), true
}

func within(base, p string) bool {
	rel, err := filepath.Rel(base, p)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(filepath.Separator))
}

func resolveVideo(ref string) string {
	c := loadConfig()
	var p string
	switch {
	case ref == "intro":
		p = introOutro().Intro
	case ref == "outro":
		p = introOutro().Outro
	case strings.HasPrefix(ref, "rhyme:") && filepath.IsAbs(filepath.FromSlash(ref[6:])):
		// a rhyme from a pasted folder: only a video, only on the NAS
		p = filepath.Clean(filepath.FromSlash(ref[6:]))
		if !allowed(p) || !videoExts[strings.ToLower(filepath.Ext(p))] {
			return ""
		}
	case strings.HasPrefix(ref, "rhyme:"):
		base := cleanPath(c.RhymesFolder)
		if base == "" {
			return ""
		}
		p = filepath.Clean(filepath.Join(base, filepath.FromSlash(ref[6:])))
		if !within(base, p) {
			return ""
		}
	case strings.HasPrefix(ref, "job:"): // a comp: its local copy while kept, else the NAS copy
		id, _ := strconv.Atoi(ref[4:])
		qMu.Lock()
		for _, j := range queue {
			if j.ID == id && j.Output != "" {
				if p = j.LocalPath; !isFile(p) {
					p = j.OutputPath
				}
			}
		}
		qMu.Unlock()
	case strings.HasPrefix(ref, "output:"): // output:<lang>/<file> or output:<file>
		parts := strings.SplitN(ref[7:], "/", 2)
		if len(parts) == 2 {
			p = filepath.Join(outputFolder(), filepath.Base(parts[0]), filepath.Base(parts[1]))
		} else {
			p = filepath.Join(outputFolder(), filepath.Base(parts[0]))
		}
	}
	if isFile(p) {
		return p
	}
	return ""
}

// ---------------------------------------------------------------- http
func writeJSON(w http.ResponseWriter, v any, code int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	json.NewEncoder(w).Encode(v)
}

func serveFile(w http.ResponseWriter, r *http.Request, p, ctype string) {
	f, err := os.Open(p)
	if err != nil {
		http.NotFound(w, r)
		return
	}
	defer f.Close()
	st, _ := f.Stat()
	w.Header().Set("Content-Type", ctype)
	w.Header().Set("Cache-Control", "no-cache")
	http.ServeContent(w, r, "", st.ModTime(), f) // handles Range requests for video seeking
}

func routes() *http.ServeMux {
	m := http.NewServeMux()
	m.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, map[string]any{"ok": true, "version": appVersion}, 200)
	})
	m.HandleFunc("/api/config", func(w http.ResponseWriter, r *http.Request) {
		if r.Method == http.MethodPost {
			var in map[string]any
			json.NewDecoder(r.Body).Decode(&in)
			c := loadConfig()
			str := func(k string, dst *string, path bool) {
				if v, ok := in[k].(string); ok {
					if path {
						v = cleanPath(v)
					}
					*dst = v
				}
			}
			str("rhymes_folder", &c.RhymesFolder, true)
			str("intro", &c.Intro, true)
			str("outro", &c.Outro, true)
			str("intro_outro_folder", &c.IOFolder, true)
			str("output_folder", &c.OutputFolder, true)
			str("ffmpeg_path", &c.FfmpegPath, true)
			str("resolution", &c.Resolution, false)
			str("fps", &c.Fps, false)
			str("transition", &c.Transition, false)
			str("quality", &c.Quality, false)
			str("cut_inner", &c.CutInner, false)
			str("encoder", &c.Encoder, false)
			if v, ok := in["parallel"].(float64); ok && v >= 1 && v <= 4 {
				c.Parallel = int(v)
			}
			if v, ok := in["fade_seconds"].(float64); ok {
				c.FadeSeconds = v
			}
			if err := saveConfig(c); err != nil {
				writeJSON(w, map[string]any{"error": err.Error()}, 500)
				return
			}
			writeJSON(w, map[string]any{"ok": true}, 200)
			return
		}
		c := loadConfig()
		out := ""
		if c.RhymesFolder != "" || c.OutputFolder != "" {
			out = outputFolder()
		}
		io := introOutro()
		writeJSON(w, map[string]any{"config": c, "status": map[string]any{
			"ffmpeg": ffmpegExe() != "", "rhymes_ok": isDir(cleanPath(c.RhymesFolder)),
			"intro_ok": io.Intro != "", "outro_ok": io.Outro != "", "output_folder": out,
			"intro_path": io.Intro, "outro_path": io.Outro, "intro_auto": io.IntroAuto, "outro_auto": io.OutroAuto,
			"io_folder_ok": isDir(cleanPath(c.IOFolder)), "version": appVersion,
			"gpu": gpuName(), "share_ok": shareOK(),
		}}, 200)
	})
	m.HandleFunc("/api/rhymes", func(w http.ResponseWriter, r *http.Request) {
		items, err := listRhymes()
		if err != nil {
			writeJSON(w, map[string]any{"error": err.Error()}, 400)
			return
		}
		langs := languages(items)
		go warmThumbs(items)
		writeJSON(w, map[string]any{"items": items, "langs": langs, "next_names": nextNames(langs)}, 200)
	})
	// a pasted folder: every video in it (and its subfolders), all in one language
	m.HandleFunc("/api/folder", func(w http.ResponseWriter, r *http.Request) {
		dir, err := localDir(r.URL.Query().Get("path"))
		if err != nil {
			writeJSON(w, map[string]any{"error": err.Error()}, 400)
			return
		}
		lang := strings.TrimSpace(badChars.ReplaceAllString(r.URL.Query().Get("lang"), " "))
		if lang == "" {
			lang = filepath.Base(dir)
		}
		items := scanRhymes(dir, lang)
		go warmThumbs(items)
		writeJSON(w, map[string]any{"path": dir, "lang": lang, "items": items, "truncated": len(items) >= maxScan,
			"next_name": nextNames([]Lang{{Name: lang}})[lang]}, 200)
	})
	m.HandleFunc("/api/info", func(w http.ResponseWriter, r *http.Request) {
		p := resolveVideo(r.URL.Query().Get("ref"))
		if p == "" {
			writeJSON(w, map[string]any{"error": "not found"}, 404)
			return
		}
		writeJSON(w, probe(p), 200)
	})
	m.HandleFunc("/api/detect", func(w http.ResponseWriter, r *http.Request) {
		ref := r.URL.Query().Get("ref")
		p := resolveVideo(ref)
		if p == "" || !strings.HasPrefix(ref, "rhyme:") {
			writeJSON(w, map[string]any{"error": "not found"}, 404)
			return
		}
		writeJSON(w, detect(p), 200)
	})
	m.HandleFunc("/thumb", func(w http.ResponseWriter, r *http.Request) {
		p := resolveVideo(r.URL.Query().Get("ref"))
		if p == "" {
			http.NotFound(w, r)
			return
		}
		t := thumbnail(p)
		if t == "" {
			http.NotFound(w, r)
			return
		}
		serveFile(w, r, t, "image/jpeg")
	})
	m.HandleFunc("/video", func(w http.ResponseWriter, r *http.Request) {
		p := resolveVideo(r.URL.Query().Get("ref"))
		if p == "" {
			http.NotFound(w, r)
			return
		}
		ct := map[string]string{".webm": "video/webm", ".mov": "video/quicktime"}[strings.ToLower(filepath.Ext(p))]
		if ct == "" {
			ct = "video/mp4"
		}
		if r.URL.Query().Get("download") == "1" {
			name := filepath.Base(p)
			w.Header().Set("Content-Disposition", fmt.Sprintf(`attachment; filename="%s"; filename*=UTF-8''%s`,
				asciiName(name), url.PathEscape(name)))
		}
		serveFile(w, r, p, ct)
	})
	queueRoutes(m)
	return m
}

func main() {
	exe, _ := os.Executable()
	appDir = filepath.Dir(exe)
	// --dir <folder>  where config.json, .cache and work/ live (the launchd service
	//                 points this at ~/Library/Application Support/CompCreator)
	// --addr <h:p>    listen address, default 127.0.0.1:8765
	addr := defaultAddr
	for i := 1; i < len(os.Args); i++ {
		switch a := os.Args[i]; {
		case a == "--dir" && i+1 < len(os.Args):
			i++
			appDir = os.Args[i]
		case a == "--addr" && i+1 < len(os.Args):
			i++
			addr = os.Args[i]
		}
	}
	if runtime.GOOS == "darwin" {
		browseRoots = []string{mountsDir}
	}
	os.MkdirAll(appDir, 0755)
	cfgPath = filepath.Join(appDir, "config.json")
	cacheDir = filepath.Join(appDir, ".cache")
	thumbDir = filepath.Join(cacheDir, "thumbs")
	infoPath = filepath.Join(cacheDir, "info.json")
	workDir = filepath.Join(appDir, "work")
	os.MkdirAll(thumbDir, 0755)
	os.MkdirAll(workDir, 0755)
	if b, err := os.ReadFile(infoPath); err == nil {
		json.Unmarshal(b, &infoCache)
	}
	loadDetectCache()
	loadUsage()
	go detectGPU()
	loadQueue()
	go sweepLoop()

	ln, err := net.Listen("tcp", addr)
	if err != nil {
		// launchd restarts us; another copy (or something else) holds the port.
		fmt.Println("Comp Creator engine: cannot listen on", addr+":", err)
		os.Exit(1)
	}
	fmt.Println("Comp Creator engine", appVersion, "listening on", addr, "- data in", appDir)
	if ffmpegExe() == "" {
		fmt.Println("!! " + ffmpegMissing())
	}
	if err := http.Serve(ln, routes()); err != nil {
		fmt.Println(err)
		os.Exit(1)
	}
}
