package main

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// setup points the engine at a temp data dir with a library and an output root.
func setup(t *testing.T) (lib, out string) {
	t.Helper()
	appDir = t.TempDir()
	cfgPath = filepath.Join(appDir, "config.json")
	cacheDir = filepath.Join(appDir, ".cache")
	thumbDir = filepath.Join(cacheDir, "thumbs")
	workDir = filepath.Join(appDir, "work")
	os.MkdirAll(thumbDir, 0755)
	os.MkdirAll(workDir, 0755)
	lib, out = filepath.Join(appDir, "lib"), filepath.Join(appDir, "nas", "Comps Outs")
	os.MkdirAll(filepath.Join(lib, "Hindi"), 0755)
	c := defaults()
	c.RhymesFolder, c.OutputFolder = lib, out
	saveConfig(c)
	qMu.Lock()
	queue, reserved, nextID = nil, map[string]bool{}, 1
	qMu.Unlock()
	useMap = map[string]usage{}
	return
}

func TestCleanTitle(t *testing.T) {
	for in, want := range map[string]string{
		"Hindi/Aaye Re Aaye Tiye_Hindi.mp4":     "Aaye Re Aaye Tiye",
		"Hindi/Aakash Gire - Hindi.mp4":         "Aakash Gire",
		"Hindi/Machli Final HD v2.mp4":          "Machli",
		"Hindi/Lakdi Ki Kathi (with intro).mp4": "Lakdi Ki Kathi (with intro)",
		"Hindi/Chanda Mama (HD).mp4":            "Chanda Mama",
	} {
		if got := cleanTitle(in, "Hindi"); got != want {
			t.Errorf("cleanTitle(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestChaptersFoldIntroIntoFirstRhyme(t *testing.T) {
	refs := []string{"intro", "rhyme:Hindi/Aakash Gire_Hindi.mp4", "rhyme:Hindi/Aam Chori.mp4", "outro"}
	clips := []clip{{length: 5}, {length: 187}, {length: 120}, {length: 4}}
	got := chapters(refs, clips, "Hindi")
	if want := "0:00 Aakash Gire\n3:12 Aam Chori"; got != want {
		t.Errorf("chapters = %q, want %q", got, want)
	}
}

func TestBuildArgsVideoToolboxLetterboxAndSilence(t *testing.T) {
	c := defaults()
	clips := []clip{
		{path: "a.mp4", info: Info{Width: 1280, Height: 720, HasAudio: true, Fps: 25}, length: 10},
		{path: "b.mp4", info: Info{Width: 1920, Height: 1080, HasAudio: false, Fps: 30}, start: 3, length: 8},
	}
	a := strings.Join(buildArgs(clips, c, "h264_videotoolbox", "out.mp4"), " ")
	for _, want := range []string{
		"-c:v h264_videotoolbox -q:v 68", // quality "high" from defaults()
		"pad=1920:1080",                  // 720p clip letterboxed, never stretched
		"anullsrc",                       // silent clip gets a silent track
		"-ss 3.000 -t 8.000 -i b.mp4",    // baked-in intro cut
		"concat=n=2:v=1:a=1",
	} {
		if !strings.Contains(a, want) {
			t.Errorf("args missing %q:\n%s", want, a)
		}
	}
	if strings.Count(a, "pad=") != 1 {
		t.Errorf("a clip already 1920x1080 should not be rescaled:\n%s", a)
	}
}

func TestFilingCopiesToNasWithTimestamps(t *testing.T) {
	_, out := setup(t)
	local := filepath.Join(workDir, "1", "Hindi Comp.mp4")
	os.MkdirAll(filepath.Dir(local), 0755)
	os.WriteFile(local, []byte("video-bytes"), 0644)
	j := &QJob{ID: 1, State: "running", Refs: []string{"rhyme:Hindi/A.mp4", "rhyme:Hindi/A.mp4", "outro"},
		LocalPath: local, OutputPath: filepath.Join(out, "Hindi", "Hindi Comp.mp4"), Chapters: "0:00 A", Output: "job:1"}
	queue = []*QJob{j}

	fileJob(j)

	if j.State != "filed" {
		t.Fatalf("state = %s (%s), want filed", j.State, j.Message)
	}
	if b, _ := os.ReadFile(filepath.Join(out, "Hindi", "Hindi Comp.mp4")); string(b) != "video-bytes" {
		t.Errorf("NAS copy = %q", b)
	}
	if b, _ := os.ReadFile(filepath.Join(out, "Hindi", "Hindi Comp - Timestamps.txt")); string(b) != "0:00 A" {
		t.Errorf("timestamps = %q", b)
	}
	if _, err := os.Stat(filepath.Join(out, "Hindi", "Hindi Comp.mp4.part")); err == nil {
		t.Error(".part file left behind")
	}
	if u := usageOf("Hindi/A.mp4"); u.Count != 1 {
		t.Errorf("usage count = %d, want 1 (a rhyme twice in one comp counts once)", u.Count)
	}
	if !isFile(local) {
		t.Error("local copy should be kept until the sweep")
	}
}

func TestFilingDoesNotOverwriteAComp(t *testing.T) {
	_, out := setup(t)
	os.MkdirAll(filepath.Join(out, "Hindi"), 0755)
	os.WriteFile(filepath.Join(out, "Hindi", "C.mp4"), []byte("older"), 0644)
	local := filepath.Join(workDir, "1", "C.mp4")
	os.MkdirAll(filepath.Dir(local), 0755)
	os.WriteFile(local, []byte("newer"), 0644)
	j := &QJob{ID: 1, LocalPath: local, OutputPath: filepath.Join(out, "Hindi", "C.mp4")}
	queue = []*QJob{j}

	fileJob(j)

	if b, _ := os.ReadFile(filepath.Join(out, "Hindi", "C.mp4")); string(b) != "older" {
		t.Error("existing comp was overwritten")
	}
	if j.Name != "C (2).mp4" || j.State != "filed" {
		t.Errorf("name=%q state=%q", j.Name, j.State)
	}
}

func TestFilingWaitsWhenShareIsDown(t *testing.T) {
	lib, out := setup(t)
	os.RemoveAll(lib) // share unmounted
	local := filepath.Join(workDir, "1", "C.mp4")
	os.MkdirAll(filepath.Dir(local), 0755)
	os.WriteFile(local, []byte("x"), 0644)
	j := &QJob{ID: 1, LocalPath: local, OutputPath: filepath.Join(out, "Hindi", "C.mp4")}
	queue = []*QJob{j}

	fileJob(j)

	if j.State != "not_on_nas" || !isFile(local) {
		t.Errorf("state=%q local kept=%v; want not_on_nas with the local copy kept", j.State, isFile(local))
	}
}

func TestSweepRemovesOldLocalCopiesOnly(t *testing.T) {
	setup(t)
	mk := func(id string) string {
		p := filepath.Join(workDir, id, "c.mp4")
		os.MkdirAll(filepath.Dir(p), 0755)
		os.WriteFile(p, []byte("x"), 0644)
		return p
	}
	old, fresh, stranded := mk("1"), mk("2"), mk("3")
	now := float64(time.Now().Unix())
	queue = []*QJob{
		{ID: 1, State: "filed", LocalPath: old, Filed: now - 49*3600, Added: now - 50*3600},
		{ID: 2, State: "filed", LocalPath: fresh, Filed: now - 3600, Added: now - 2*3600},
		{ID: 3, State: "not_on_nas", LocalPath: stranded, Added: now},
	}
	sweep()
	if isFile(old) || !isFile(fresh) || !isFile(stranded) {
		t.Errorf("old gone=%v fresh kept=%v not_on_nas kept=%v", !isFile(old), isFile(fresh), isFile(stranded))
	}
}

func TestSweepForgetsFinishedCompsAfterAWeek(t *testing.T) {
	setup(t)
	now := float64(time.Now().Unix())
	queue = []*QJob{
		{ID: 1, State: "filed", Added: now - 8*86400},
		{ID: 2, State: "filed", Added: now - 86400},
		{ID: 3, State: "not_on_nas", Added: now - 30*86400}, // still needs filing: kept
		{ID: 4, State: "error", Added: now - 9*86400},
	}
	sweep()
	ids := []int{}
	for _, j := range queue {
		ids = append(ids, j.ID)
	}
	if len(ids) != 2 || ids[0] != 2 || ids[1] != 3 {
		t.Errorf("kept %v, want [2 3]", ids)
	}
}

func TestNextNames(t *testing.T) {
	_, out := setup(t)
	os.MkdirAll(filepath.Join(out, "Hindi"), 0755)
	os.WriteFile(filepath.Join(out, "Hindi", "Hindi Rhymes Compilation 4.mp4"), nil, 0644)
	queue = []*QJob{{Name: "Hindi Rhymes Compilation 6"}}
	got := nextNames([]Lang{{Name: "Hindi"}, {Name: "Gujarati"}})
	if got["Hindi"] != "Hindi Rhymes Compilation 7" || got["Gujarati"] != "Gujarati Rhymes Compilation 1" {
		t.Errorf("nextNames = %v", got)
	}
}
