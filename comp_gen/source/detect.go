package main

import (
	"encoding/json"
	"fmt"
	"math"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"time"
)

// Strict patterns: used to hide intro/outro files that sit inside the rhymes folder.
// Matches "intro"/"outro" as a word ("Intro.mp4", "TMKOC_outro", "Intro & Outro" folder)
// but not "Introduction".
var reIntro = regexp.MustCompile(`(?i)(^|[^a-z])(intro|opening ?title)([^a-z]|$)`)
var reOutro = regexp.MustCompile(`(?i)(^|[^a-z])(outro|end ?screen|end ?slate)([^a-z]|$)`)

// Loose patterns: used to tell intro from outro inside an intro/outro folder.
var reIntroLoose = regexp.MustCompile(`(?i)(^|[^a-z])(intro|opening|title|start)([^a-z]|$)`)
var reOutroLoose = regexp.MustCompile(`(?i)(^|[^a-z])(outro|end ?slate|end ?screen|end ?card|ending|end|closing|subscribe)([^a-z]|$)`)
var rePreferred = regexp.MustCompile(`(?i)use ?this|final`)

var reWords = regexp.MustCompile(`[\p{L}\p{N}]+`)

// relHasMatch: a folder named intro/outro, or a short file name (max 4 words) such as
// "Intro", "TMKOC Intro", "Outro_v2". Long rhyme titles that only mention the word are not matched.
func relHasMatch(rel string, re *regexp.Regexp) bool {
	parts := strings.Split(filepath.ToSlash(rel), "/")
	for i, part := range parts {
		if i == len(parts)-1 {
			stem := strings.TrimSuffix(part, filepath.Ext(part))
			if re.MatchString(stem) && len(reWords.FindAllString(stem, -1)) <= 4 {
				return true
			}
			continue
		}
		if re.MatchString(part) {
			return true
		}
	}
	return false
}

// classify says whether a video (inside an intro/outro folder) is the intro or the outro.
func classify(rel string) (isI, isO bool) {
	stem := strings.TrimSuffix(filepath.Base(rel), filepath.Ext(rel))
	isI, isO = reIntroLoose.MatchString(stem), reOutroLoose.MatchString(stem)
	if isI && isO { // e.g. "Intro Outro combined" - ambiguous
		return false, false
	}
	if !isI && !isO { // fall back to the folder name ("Intro" folder / "Outro" folder)
		dir := filepath.ToSlash(filepath.Dir(rel))
		fi, fo := reIntro.MatchString(dir), reOutro.MatchString(dir)
		if fi != fo {
			return fi, fo
		}
	}
	return
}

// ---------------------------------------------------------------- auto intro/outro files
type ioPaths struct {
	Intro, Outro         string
	IntroAuto, OutroAuto bool
	Folder               string // intro/outro folder that was used, if any
	at                   time.Time
	key                  string
}

var (
	ioMu    sync.Mutex
	ioCache ioPaths
)

// pick finds intro and outro videos in dir. strict = dir is the rhymes folder, so only
// files clearly named/foldered as intro/outro are considered.
func pick(dir string, strict bool) (intro, outro string) {
	score := func(rel string) int {
		s := 1000 - len(rel)
		if rePreferred.MatchString(filepath.Base(rel)) {
			s += 10000
		}
		return s
	}
	bi, bo := -1, -1
	walkVideos(dir, func(full, rel string) bool {
		if strict && !relHasMatch(rel, reIntro) && !relHasMatch(rel, reOutro) {
			return true
		}
		isI, isO := classify(rel)
		if isI && score(rel) > bi {
			intro, bi = full, score(rel)
		}
		if isO && score(rel) > bo {
			outro, bo = full, score(rel)
		}
		return true
	})
	return
}

// introOutro returns the intro/outro to use, in this order:
// intro/outro file set in Settings -> the Intro & Outro folder -> files named intro/outro in the rhymes folder.
func introOutro() ioPaths {
	c := loadConfig()
	key := c.RhymesFolder + "|" + c.IOFolder + "|" + c.Intro + "|" + c.Outro
	ioMu.Lock()
	if ioCache.key == key && time.Since(ioCache.at) < 5*time.Second {
		r := ioCache
		ioMu.Unlock()
		return r
	}
	ioMu.Unlock()

	r := ioPaths{key: key, at: time.Now()}
	if p := cleanPath(c.Intro); isFile(p) {
		r.Intro = p
	}
	if p := cleanPath(c.Outro); isFile(p) {
		r.Outro = p
	}
	try := func(dir string, strict bool) {
		if (r.Intro != "" && r.Outro != "") || !isDir(dir) {
			return
		}
		i, o := pick(dir, strict)
		if r.Intro == "" && i != "" {
			r.Intro, r.IntroAuto = i, true
		}
		if r.Outro == "" && o != "" {
			r.Outro, r.OutroAuto = o, true
		}
		if !strict && (i != "" || o != "") {
			r.Folder = dir
		}
	}
	try(cleanPath(c.IOFolder), false)
	try(cleanPath(c.Intro), false) // a folder pasted into the intro/outro box also works
	try(cleanPath(c.Outro), false)
	try(cleanPath(c.RhymesFolder), true)
	ioMu.Lock()
	ioCache = r
	ioMu.Unlock()
	return r
}

var reCompsDir = regexp.MustCompile(`(?i)^(comps?|comps? ?outs?|comp ?outputs?|outputs?|exports?|renders?)$`)

// skipDir: folders that never hold rhymes - hidden folders, the comps output folder,
// and this app's own folder.
func skipDir(p, name string) bool {
	if strings.HasPrefix(name, ".") || reCompsDir.MatchString(name) ||
		strings.HasPrefix(strings.ToLower(name), "compcreator") {
		return true
	}
	abs, _ := filepath.Abs(p)
	if strings.EqualFold(abs, outputFolder()) || strings.EqualFold(abs, appDir) {
		return true
	}
	return isFile(filepath.Join(p, "CompCreator.exe")) || isFile(filepath.Join(p, "CompCreator"))
}

// walkVideos calls fn for every video under folder; fn returns false to stop.
func walkVideos(folder string, fn func(full, rel string) bool) {
	filepath.WalkDir(folder, func(p string, d os.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() {
			if p != folder && skipDir(p, d.Name()) {
				return filepath.SkipDir
			}
			return nil
		}
		if videoExts[strings.ToLower(filepath.Ext(d.Name()))] {
			rel, _ := filepath.Rel(folder, p)
			if !fn(p, filepath.ToSlash(rel)) {
				return filepath.SkipAll
			}
		}
		return nil
	})
}

// ---------------------------------------------------------------- detect intro/outro inside a rhyme
type Detect struct {
	Intro    bool    `json:"intro"` // rhyme starts with the intro
	Outro    bool    `json:"outro"` // rhyme ends with the outro
	IntroLen float64 `json:"intro_len"`
	OutroLen float64 `json:"outro_len"`
	Duration float64 `json:"duration"`
}

var (
	detMu    sync.Mutex
	detCache = map[string]Detect{}
	detSem   = make(chan struct{}, 2)
)

func loadDetectCache() {
	if b, err := os.ReadFile(filepath.Join(cacheDir, "detect.json")); err == nil {
		json.Unmarshal(b, &detCache)
	}
}

// frame grabs a tiny 32x18 grayscale frame at time t.
func frame(p string, t float64) []byte {
	out, err := ffCmd("-hide_banner", "-loglevel", "error", "-ss", fmt.Sprintf("%.3f", t), "-i", p,
		"-frames:v", "1", "-vf", "scale=32:18,format=gray", "-f", "rawvideo", "-").Output()
	if err != nil || len(out) != 32*18 {
		return nil
	}
	return out
}

func stats(a []byte) (mean, sd float64) {
	for _, v := range a {
		mean += float64(v)
	}
	mean /= float64(len(a))
	for _, v := range a {
		sd += (float64(v) - mean) * (float64(v) - mean)
	}
	return mean, math.Sqrt(sd / float64(len(a)))
}

func diff(a, b []byte) float64 {
	s := 0.0
	for i := range a {
		s += math.Abs(float64(a[i]) - float64(b[i]))
	}
	return s / float64(len(a)) / 255
}

// same reports whether video p shows the same picture as ref at the given moments.
// pOffset shifts the moment in p (used for the outro at the end of the rhyme).
func same(p string, pOffset float64, ref string, refDur float64) bool {
	checked := 0
	for _, f := range []float64{0.2, 0.5, 0.8} {
		t := refDur * f
		a := frame(ref, t)
		if a == nil {
			continue
		}
		if _, sd := stats(a); sd < 6 { // plain/black frame: not useful for matching
			continue
		}
		b := frame(p, pOffset+t)
		if b == nil || diff(a, b) > 0.09 {
			return false
		}
		checked++
	}
	return checked > 0
}

func detect(p string) Detect {
	io := introOutro()
	info := probe(p)
	d := Detect{Duration: info.Duration}
	var iLen, oLen float64
	if io.Intro != "" {
		iLen = probe(io.Intro).Duration
	}
	if io.Outro != "" {
		oLen = probe(io.Outro).Duration
	}
	d.IntroLen, d.OutroLen = iLen, oLen
	key := fileKey(p) + "|" + keyOr(io.Intro) + "|" + keyOr(io.Outro)
	detMu.Lock()
	if r, ok := detCache[key]; ok {
		detMu.Unlock()
		return r
	}
	detMu.Unlock()

	detSem <- struct{}{}
	if iLen > 0 && info.Duration > iLen+1 {
		d.Intro = same(p, 0, io.Intro, iLen)
	}
	if oLen > 0 && info.Duration > oLen+1 {
		d.Outro = same(p, info.Duration-oLen, io.Outro, oLen)
	}
	<-detSem

	detMu.Lock()
	detCache[key] = d
	b, _ := json.Marshal(detCache)
	detMu.Unlock()
	os.WriteFile(filepath.Join(cacheDir, "detect.json"), b, 0644)
	return d
}

func keyOr(p string) string {
	if p == "" {
		return "-"
	}
	return fileKey(p)
}

// cutFor returns how many seconds to cut from the start and end of a rhyme.
func cutFor(p string, mode string) (start, end float64) {
	if mode == "off" {
		return 0, 0
	}
	d := detect(p)
	cutI, cutO := d.Intro, d.Outro
	if mode == "always" {
		cutI, cutO = d.IntroLen > 0, d.OutroLen > 0
	}
	if cutI {
		start = d.IntroLen
	}
	if cutO {
		end = d.OutroLen
	}
	if d.Duration-start-end < 1 { // never cut a rhyme down to nothing
		return 0, 0
	}
	return
}
