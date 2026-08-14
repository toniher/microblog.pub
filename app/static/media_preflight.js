// Client-side preflight for video/audio uploads: previews duration/dimensions
// instantly (no server round-trip) via the browser's own decoder, and warns
// before submit if this browser can't play the file at all.
//
// This only tests the *posting* browser (e.g. Safari plays HEVC that
// Firefox/Chrome reject) — it's a fast first warning, not authoritative.
// The server-side classifier (app/ffmpeg.py) has the final say and returns
// a 422 naming the specific problem if the file is rejected on upload.
(function () {
    var fileInput = document.getElementById("files");
    if (fileInput == null) {
        return;
    }

    function formatDuration(seconds) {
        var total = Math.round(seconds);
        var minutes = Math.floor(total / 60);
        var secs = total % 60;
        return minutes + ":" + (secs < 10 ? "0" : "") + secs;
    }

    function ensureContainer() {
        var container = document.getElementById("media-preflight");
        if (container == null) {
            container = document.createElement("div");
            container.id = "media-preflight";
            fileInput.parentNode.appendChild(container);
        }
        container.innerHTML = "";
        return container;
    }

    function preflightOne(file, container) {
        var isVideo = file.type.indexOf("video/") === 0;
        var isAudio = file.type.indexOf("audio/") === 0;
        if (!isVideo && !isAudio) {
            return;
        }

        var row = document.createElement("p");
        row.textContent = file.name + ": checking playback in this browser…";
        container.appendChild(row);

        var probe = document.createElement(isVideo ? "video" : "audio");
        probe.preload = "metadata";
        var objectUrl = URL.createObjectURL(file);
        probe.src = objectUrl;

        function cleanup() {
            URL.revokeObjectURL(objectUrl);
        }

        probe.addEventListener("loadedmetadata", function () {
            var details = file.name + ": ";
            if (isVideo) {
                details += probe.videoWidth + "x" + probe.videoHeight + ", ";
            }
            details += formatDuration(probe.duration) + " — plays in this browser";
            row.textContent = details;
            cleanup();
        });

        probe.addEventListener("error", function () {
            row.textContent = (
                file.name + ": this browser could not play this file " +
                "(unsupported codec/container) — it may be rejected on " +
                "upload. Re-encoding as H.264/AAC in an MP4 is the safest bet."
            );
            cleanup();
        });
    }

    fileInput.addEventListener("change", function (e) {
        var container = ensureContainer();
        for (var i = 0; i < e.target.files.length; i++) {
            preflightOne(e.target.files[i], container);
        }
    });
})();
