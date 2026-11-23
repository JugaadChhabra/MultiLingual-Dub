package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"
)

// ---------------------------------------------------------------- rhyme usage
// How many filed comps each rhyme went into, and when last. Kept apart from the
// queue because "Clear finished" empties the queue and this must survive it.
type usage struct {
	Count int     `json:"count"`
	Last  float64 `json:"last"`
}

var (
	useMu  sync.Mutex
	useMap = map[string]usage{} // key: rhyme rel path ("Hindi/Aakash Gire.mp4")
)

func usagePath() string { return filepath.Join(cacheDir, "usage.json") }

func loadUsage() {
	if b, err := os.ReadFile(usagePath()); err == nil {
		json.Unmarshal(b, &useMap)
	}
}

func usageOf(rel string) usage {
	useMu.Lock()
	defer useMu.Unlock()
	return useMap[rel]
}

// recordUsage counts a filed comp's rhymes. A rhyme used twice in one comp counts once.
func recordUsage(refs []string) {
	now := float64(time.Now().Unix())
	seen := map[string]bool{}
	useMu.Lock()
	for _, r := range refs {
		if rel, ok := strings.CutPrefix(r, "rhyme:"); ok && !seen[rel] {
			seen[rel] = true
			u := useMap[rel]
			u.Count, u.Last = u.Count+1, now
			useMap[rel] = u
		}
	}
	b, _ := json.Marshal(useMap)
	useMu.Unlock()
	os.WriteFile(usagePath(), b, 0644)
}

// ---------------------------------------------------------------- suggested names
// nextNames suggests "<Lang> Rhymes Compilation <n+1>" per language, n being the
// highest number already filed in that language's folder or waiting in the queue.
func nextNames(langs []Lang) map[string]string {
	out := map[string]string{}
	qMu.Lock()
	queued := []string{}
	for _, j := range queue {
		queued = append(queued, j.Name)
	}
	qMu.Unlock()
	root := cleanPath(loadConfig().OutputFolder)
	if root == "" && shareOK() {
		root = outputFolder()
	}
	for _, l := range langs {
		base := strings.TrimSpace(l.Name + " Rhymes Compilation")
		re := regexp.MustCompile(`(?i)^` + regexp.QuoteMeta(base) + ` (\d+)\b`)
		hi := 0
		names := append([]string{}, queued...)
		if root != "" {
			dir := root
			if l.Name != "" {
				dir = filepath.Join(root, l.Name)
			}
			if ents, err := os.ReadDir(dir); err == nil {
				for _, e := range ents {
					names = append(names, e.Name())
				}
			}
		}
		for _, n := range names {
			if m := re.FindStringSubmatch(n); m != nil {
				if k, _ := strconv.Atoi(m[1]); k > hi {
					hi = k
				}
			}
		}
		out[l.Name] = fmt.Sprintf("%s %d", base, hi+1)
	}
	return out
}

// ---------------------------------------------------------------- background work
var warming atomic.Bool

const finishedKeepDays = 7

// warmThumbs renders missing thumbnails after a library listing, so opening a
// language does not wait on dozens of ffmpeg calls over SMB. One pass at a time.
func warmThumbs(items []Rhyme) {
	if !warming.CompareAndSwap(false, true) {
		return
	}
	defer warming.Store(false)
	for _, it := range items {
		if p := resolveVideo("rhyme:" + it.Rel); p != "" {
			thumbnail(p)
		}
	}
}

// sweepLoop deletes local copies of comps filed more than local_retention_hours
// ago (the NAS copy is the deliverable), and work folders no job claims. It also
// drops finished comps from the list a week after they were added, so the pane
// never needs a "Clear" button. A comp still waiting to be filed is never dropped.
func sweepLoop() {
	for {
		sweep()
		time.Sleep(30 * time.Minute)
	}
}

func sweep() {
	hrs := loadConfig().LocalRetentionHours
	if hrs <= 0 {
		hrs = 48
	}
	cutoff := float64(time.Now().Unix()) - hrs*3600
	claimed := map[string]bool{}
	forget := float64(time.Now().Unix()) - finishedKeepDays*86400
	qMu.Lock()
	keep := queue[:0]
	for _, j := range queue {
		if (j.State == "filed" || j.State == "error" || j.State == "cancelled") && j.Added < forget {
			removeLocal(j)
			continue
		}
		keep = append(keep, j)
	}
	queue = keep
	for _, j := range queue {
		if j.State == "filed" && j.LocalPath != "" && j.Filed < cutoff {
			removeLocal(j)
		}
		if j.LocalPath != "" {
			claimed[filepath.Dir(j.LocalPath)] = true
		}
	}
	saveQueue()
	qMu.Unlock()
	ents, _ := os.ReadDir(workDir)
	for _, e := range ents {
		d := filepath.Join(workDir, e.Name())
		if info, err := e.Info(); err == nil && !claimed[d] && float64(info.ModTime().Unix()) < cutoff {
			os.RemoveAll(d)
		}
	}
}

// asciiName: a plain fallback for Content-Disposition's filename=.
func asciiName(s string) string {
	b := []rune{}
	for _, r := range s {
		if r < 32 || r > 126 || r == '"' || r == '\\' {
			r = '_'
		}
		b = append(b, r)
	}
	return string(b)
}
