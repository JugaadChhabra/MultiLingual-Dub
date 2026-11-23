COMP CREATOR ENGINE - merges rhyme videos into YouTube compilations

This is the engine behind Content Hub's Compilations page. It runs on the office
Mac mini outside Docker, as a background service.

Setup and updates: see MAC_MINI_SETUP.md at the root of the repo.

  source/          Go source. Tests: cd source && go test ./...
  macos/           install.sh (installs/updates the service) and the starting config

The .exe files in this folder are the old Windows build. They are git-ignored.
