function hasAudio (video) {
    // Server-provided answer (local attachments carry Upload.has_audio, see
    // attachments.html) takes priority over sniffing.
    if (video.dataset.hasAudio === "true") {
        return true;
    }
    if (video.dataset.hasAudio === "false") {
        return false;
    }

    // No server-provided answer (a remote attachment) — fall back to
    // sniffing. None of these properties exist in Chromium, so an
    // indeterminate result must default to "has audio" rather than silently
    // stripping controls from every short video.
    if (typeof video.mozHasAudio === 'boolean') {
        return video.mozHasAudio;
    }
    if (typeof video.webkitAudioDecodedByteCount === 'number') {
        return video.webkitAudioDecodedByteCount > 0;
    }
    if (video.audioTracks) {
        return video.audioTracks.length > 0;
    }
    return true;
}

function setVideoInGIFMode(video) {
    if (!hasAudio(video)) {
        if (typeof video.loop == 'boolean' && video.duration <= 10.0) {
            video.classList.add("video-gif-mode");
            video.loop = true;
            video.controls = false;
            video.addEventListener("mouseover", () => {
                video.play();
            })
            video.addEventListener("mouseleave", () => {
                video.pause();
            })
        }
    };
}

var items = document.getElementsByTagName("video")
for (var i = 0; i < items.length; i++) {
    if (items[i].duration) {
        setVideoInGIFMode(items[i]);
    } else {
        items[i].addEventListener("loadeddata", function() {
            setVideoInGIFMode(this);
        });
    }
}
