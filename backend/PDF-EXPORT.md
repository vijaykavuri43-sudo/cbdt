# Large slip PDF exports

Authorization reference: `CR71-2026-GUDIVADA-ILLEGAL-BETTING`.

Crypto slip PDF jobs have no fixed source-file or page-count limit. Filters
select all matching source files before conservative deduplication. Missing or
unreadable images fail the job explicitly; partial evidence packs are not served.

Rendering writes individual PDF image/page objects directly to a temporary file,
then atomically publishes the finished PDF. Memory for image data is bounded to
one image; candidate metadata, deduplication identities and PDF object offsets
still grow with the number of matching records. JPEG image streams are retained
unchanged; other images use lossless original-resolution RGB and alpha streams.
Source files are never modified.

Jobs are serialized per data volume across API workers. A separate heartbeat
keeps queued jobs and long fetch/render operations alive. The browser polls until
completion without a 30-minute cutoff. Downloads use the browser's native download
manager instead of buffering the whole PDF into a JavaScript Blob. An authenticated
POST issues a ten-minute HttpOnly, SameSite=Strict cookie scoped to that job's
download path; the normal bearer-authenticated download route also still works.
Download secrets and the API bearer key do not appear in download URLs.

Capacity is finite: exports need disk space for staged images and the finished
PDF. A 128-MiB free-space reserve is checked during staging/rendering; insufficient
space fails explicitly. Derived job directories expire after 24 hours and are
cleaned when a subsequent job starts; actively heartbeating jobs are not removed.
The existing 25-MiB / 40-megapixel per-image safety checks remain. Multi-frame
images still require their original source file, rather than silent frame loss.

Before increasing VM size, check workspace disk space and observed memory use.
Large outputs may take time to fetch and need enough free space on the receiving
computer as well. A server restart interrupts in-flight jobs; stale jobs can be
retried after the heartbeat timeout (30 minutes).
