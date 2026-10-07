# Build review: local worker inherited-stdin hang

## Result

External tools launched through `id_detector.process.run_process` now receive an empty stdin.
POSIX passes `subprocess.DEVNULL`; Windows supplies an inheritable `NUL` handle and closes the
parent copy immediately after `CreateProcess` returns. This prevents the supervised worker's
stop-pipe reader from deadlocking child tools such as yt-dlp, ffmpeg and ffprobe.

## Cause

`LocalWorkerSupervisor` starts the worker with `stdin=subprocess.PIPE`. The worker has a daemon
watcher blocked on that dedicated pipe so it stops if its supervisor exits. The Windows process
runner previously copied that same standard-input handle into every child process. A child probing
the inherited synchronous pipe during startup could block behind the watcher's pending read before
it executed useful work.

The regression coverage models the pending worker read directly and also runs a real supervised
worker through a real ffmpeg decode. Suspended creation, assignment to the kill-on-close Job Object,
thread resume, timeout, cancellation and whole-tree cleanup are unchanged.
