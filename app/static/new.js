// The new post textarea
(function () {
    // Not getElementById("content"): layout.html's <main id="content"> landmark
    // shares that id and sits earlier in the DOM, so it wins the lookup.
    var ta = document.querySelector(".admin-new textarea");
    if (ta == null) {
        return;
    }

    // Helper for inserting text (emoji, markdown snippets) in the textarea.
    // Returns the offset the text landed at, so callers can re-select the
    // inserted region. `execCommand` is kept despite being deprecated because it
    // preserves the browser's native undo stack -- assigning to `ta.value`
    // destroys it -- and because it replaces the current selection, which is what
    // the "wrap the selection" markdown buttons rely on.
    function insertAtCursor (textToInsert) {
        ta.focus();
        const start = ta.selectionStart;
        const isSuccess = document.execCommand("insertText", false, textToInsert);

        // Firefox (non-standard method)
        if (!isSuccess) {
            // Credits to https://www.everythingfrontend.com/posts/insert-text-into-textarea-at-cursor-position.html
            // get current text of the input
            const value = ta.value;
            // save selection start and end position
            const end = ta.selectionEnd;
            // update the value with our text inserted
            ta.value = value.slice(0, start) + textToInsert + value.slice(end);
            // update cursor to be at the end of insertion
            ta.selectionStart = ta.selectionEnd = start + textToInsert.length;
        }
        return start;
    }
    // Emoji click callback func. The value comes from the button's own
    // `data-emoji` rather than a nested <img alt>: unicode emoji render as
    // plain text here (no twemoji asset needed), so there is no image to read.
    var ji = function (ev) {
        var value = ev.currentTarget.getAttribute("data-emoji");
        if (!value) {
            return;
        }
        insertAtCursor(value + " ");
        ta.focus()
    }
    // Enable the click for each emojis
    var items = document.getElementsByClassName("ji")
    for (var i = 0; i < items.length; i++) {
        items[i].addEventListener('click', ji);
    }

    // --- Markdown formatting bar -----------------------------------------
    // Behaviour lives in data-* attributes so the translated labels and
    // placeholders stay in components/markdown_bar.html, not here.
    var MD_SELECTOR = "[data-md-tpl], [data-md-line]";

    function applyMd (btn) {
        const value = ta.value;
        const s = ta.selectionStart;
        const e0 = ta.selectionEnd;
        const hadSelection = e0 > s;
        const ph = btn.getAttribute("data-md-ph") || "";
        const line = btn.getAttribute("data-md-line");

        if (line) {
            // A selection ending exactly on a newline must not pull in the
            // (empty) following line.
            let e = (hadSelection && value.charAt(e0 - 1) === "\n") ? e0 - 1 : e0;
            // Snap to whole lines: a line prefix is meaningless mid-line.
            const ls = value.lastIndexOf("\n", s - 1) + 1;
            let le = value.indexOf("\n", e);
            if (le === -1) {
                le = value.length;
            }
            const body = value.slice(ls, le);
            const lines = body.length ? body.split("\n") : [ph];
            const out = lines.map(function (l, i) {
                return line.replace("$n", String(i + 1)) + l;
            }).join("\n");
            const prefixLen = line.replace("$n", "1").length;

            ta.setSelectionRange(ls, le);
            const at = insertAtCursor(out);

            if (hadSelection) {
                ta.setSelectionRange(at, at + out.length);       // show what changed
            } else if (body.length) {
                const caret = at + prefixLen + (s - ls);         // keep the caret put
                ta.setSelectionRange(caret, caret);
            } else {
                // Empty line: select the placeholder so typing replaces it.
                ta.setSelectionRange(at + prefixLen, at + prefixLen + ph.length);
            }
            return;
        }

        const tpl = (btn.getAttribute("data-md-tpl") || "").replace(/\\n/g, "\n");
        const marker = tpl.indexOf("$1");
        if (marker === -1) {
            return;
        }
        const inner = hadSelection ? value.slice(s, e0) : ph;

        let pre = "";
        let post = "";
        if (btn.hasAttribute("data-md-block")) {
            // A fenced block needs an empty line on each side, unless it is
            // already at the start/end of the text or already separated.
            if (s > 0) {
                pre = value.charAt(s - 1) !== "\n"
                    ? "\n\n"
                    : (s > 1 && value.charAt(s - 2) !== "\n" ? "\n" : "");
            }
            if (e0 < value.length) {
                post = value.charAt(e0) !== "\n"
                    ? "\n\n"
                    : (e0 + 1 < value.length && value.charAt(e0 + 1) !== "\n" ? "\n" : "");
            }
        }

        const at = insertAtCursor(pre + tpl.replace("$1", inner) + post);
        // `$1` is the only substitution and nothing before it changes length, so
        // its template offset still holds -- shifted by the padding.
        const off = at + pre.length + marker;
        ta.setSelectionRange(off, off + inner.length);
    }

    var mdBar = document.querySelector(".md-bar");
    if (mdBar != null) {
        // Revealed only now that the JS driving it has run, so the buttons are
        // never dead controls.
        mdBar.hidden = false;

        // Delegated, unlike the per-button emoji loop above: one listener for a
        // dozen static buttons.
        mdBar.addEventListener("click", function (ev) {
            var btn = ev.target.closest(MD_SELECTOR);
            if (btn != null) {
                applyMd(btn);
            }
        });

        // ARIA toolbar pattern: the bar is a single tab stop and the arrow keys
        // move within it, so reaching the compose box by keyboard doesn't mean
        // tabbing past every button.
        var mdBtns = mdBar.querySelectorAll(MD_SELECTOR);
        if (mdBtns.length > 0) {
            mdBtns[0].setAttribute("tabindex", "0");
        }
        mdBar.addEventListener("keydown", function (ev) {
            var i = Array.prototype.indexOf.call(mdBtns, document.activeElement);
            if (i < 0) {
                return;
            }
            var j = -1;
            if (ev.key === "ArrowRight") { j = (i + 1) % mdBtns.length; }
            else if (ev.key === "ArrowLeft") { j = (i - 1 + mdBtns.length) % mdBtns.length; }
            else if (ev.key === "Home") { j = 0; }
            else if (ev.key === "End") { j = mdBtns.length - 1; }
            if (j < 0) {
                return;
            }
            ev.preventDefault();
            mdBtns[i].setAttribute("tabindex", "-1");
            mdBtns[j].setAttribute("tabindex", "0");
            mdBtns[j].focus();
        });
    }

    // Add new input text dynamically to allow setting an alt text on attachments
    var files = document.getElementById("files");
    var alts = document.getElementById("alts");
    if (files != null && alts != null) {
        var altPlaceholder = alts.getAttribute("data-alt-placeholder") || "Alt text for __FILENAME__";
        files.addEventListener("change", function(e) {
            // Reset the div content
            alts.innerHTML = "";

            // Add an input for each files, keyed by index (not filename, which
            // can collide across multiple selected files)
            for (var i = 0; i < e.target.files.length; i++) {
                var p = document.createElement("p");
                var altInput = document.createElement("input");
                altInput.setAttribute("type", "text");
                altInput.setAttribute("name", "alt_" + i);
                altInput.setAttribute(
                    "placeholder",
                    altPlaceholder.replace("__FILENAME__", e.target.files[i].name)
                );
                altInput.setAttribute("style", "width:95%;")
                p.appendChild(altInput);
                alts.appendChild(p);
            }
        });
    }
    // Focus at the end of the textarea
    ta.setSelectionRange(ta.value.length, ta.value.length);
    ta.focus();


    // Ctrl+Vで画像添付
    document.addEventListener('paste', async (event) => {
        var fileInput = document.getElementById('files');
        if (fileInput == null) {
            return;
        }

        const items = event.clipboardData.items;
        const dataTransfer = new DataTransfer();

        let previews = document.getElementById("files-preview");
        if (previews == null) {
            previews = document.createElement("div");
            previews.id = "files-preview";
            previews.style.display = "flex";
            previews.style.flexWrap = "wrap";
            previews.style.gap = "5px";
            fileInput.parentNode.appendChild(previews);
        }

        for (const existingFile of fileInput.files) {
            dataTransfer.items.add(existingFile);
        }

        var added = false;
        for (const item of items) {
            if (item.type.startsWith('image/')) {
                const file = item.getAsFile();
                if (!file) continue;
                added = true;

                dataTransfer.items.add(file);

                const preview = document.createElement('img');
                preview.style.maxWidth = '200px';
                preview.style.maxHeight = '200px';
                preview.style.objectFit = 'contain';
                preview.style.margin = "2px";
                const reader = new FileReader();
                reader.onload = (e) => {
                    preview.src = e.target.result;
                    previews.appendChild(preview);
                };
                reader.readAsDataURL(file);
            }
        }

        if (added) {
            fileInput.files = dataTransfer.files;
        }
    });

    // Ctrl+Enterで投稿 / Ctrl+B, Ctrl+I, Ctrl+K for markdown
    ta.addEventListener('keydown', function(event) {
        if (event.ctrlKey && event.key === 'Enter') {
            event.preventDefault();
            document.querySelector('.admin-new').submit();
            return;
        }
        // Bail on Alt/Shift so Ctrl+Shift+B (browser bookmarks bar) still works.
        if (!(event.ctrlKey || event.metaKey) || event.altKey || event.shiftKey) {
            return;
        }
        // The template stays the single source of truth for each construct.
        var btn = document.querySelector('.md-bar [data-md-key="' + event.key.toLowerCase() + '"]');
        if (btn == null) {
            return;
        }
        event.preventDefault();
        applyMd(btn);
    });
})();
