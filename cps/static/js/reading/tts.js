/* global reader */

/**
 * Text-to-speech per il reader EPUB (epub.js).
 *
 * Usa la Web Speech API del browser (window.speechSynthesis): sintesi
 * lato client, nessun dato inviato a server esterni, nessuna dipendenza
 * aggiuntiva. Legge la sezione corrente blocco per blocco (paragrafi,
 * titoli, voci di lista...), evidenzia il blocco in lettura, fa scorrere
 * il reader per seguirlo e, a fine sezione, passa alla successiva.
 *
 * Voce e velocità sono scelte dall'utente e memorizzate in localStorage.
 */
(function () {
    "use strict";

    var synth = window.speechSynthesis;
    var toggleBtn = document.getElementById("tts-toggle");
    var prevBtn = document.getElementById("tts-prev");
    var nextBtn = document.getElementById("tts-next");

    // Browser senza Web Speech API: niente TTS, nascondi il controllo.
    if (!synth || typeof window.SpeechSynthesisUtterance === "undefined") {
        [toggleBtn, prevBtn, nextBtn,
            document.getElementById("tts-settings")].forEach(function (el) {
            if (el && el.parentNode) {
                el.parentNode.removeChild(el);
            }
        });
        return;
    }

    // Selettore dei blocchi "leggibili" dentro una sezione.
    var BLOCK_SELECTOR =
        "p, li, blockquote, h1, h2, h3, h4, h5, h6, dd, dt, figcaption, td, th, pre";
    var HIGHLIGHT_CLASS = "tts-reading";
    var RATE_KEY = "calibre.reader.tts.rate";
    var VOICE_KEY = "calibre.reader.tts.voice";

    var state = "stopped"; // "stopped" | "playing" | "paused"
    var queue = []; // [{ el, text }]
    var qi = 0; // indice corrente in queue
    var highlighted = null; // elemento attualmente evidenziato
    var navByTts = false; // display() richiesto da noi, non dall'utente
    var speakSeq = 0; // token: invalida gli onend delle utterance annullate

    var rate = parseFloat(localStorage.getItem(RATE_KEY)) || 1;
    if (rate < 0.5 || rate > 2) {
        rate = 1;
    }
    var voices = [];
    var selectedVoice = null;

    // ---- Voci ----------------------------------------------------------

    function pickSavedVoice() {
        var saved = localStorage.getItem(VOICE_KEY);
        if (!saved) {
            return null;
        }
        for (var i = 0; i < voices.length; i++) {
            if (voices[i].voiceURI === saved) {
                return voices[i];
            }
        }
        return null;
    }

    function preferredVoiceForBook() {
        // Prova ad allineare la voce alla lingua del libro.
        var lang = "";
        try {
            lang = (reader.book.package.metadata.language || "").toLowerCase();
        } catch (e) { /* noop */ }
        if (lang) {
            var base = lang.split("-")[0];
            for (var i = 0; i < voices.length; i++) {
                if ((voices[i].lang || "").toLowerCase().indexOf(base) === 0) {
                    return voices[i];
                }
            }
        }
        return voices[0] || null;
    }

    function loadVoices() {
        voices = synth.getVoices() || [];
        if (!voices.length) {
            return;
        }
        selectedVoice = pickSavedVoice() || preferredVoiceForBook();
        populateVoiceSelect();
    }

    function populateVoiceSelect() {
        var sel = document.getElementById("tts-voice");
        if (!sel) {
            return;
        }
        sel.innerHTML = "";
        voices.forEach(function (voice) {
            var opt = document.createElement("option");
            opt.value = voice.voiceURI;
            opt.textContent = voice.name + " (" + voice.lang + ")";
            if (selectedVoice && voice.voiceURI === selectedVoice.voiceURI) {
                opt.selected = true;
            }
            sel.appendChild(opt);
        });
    }

    // I browser popolano le voci in modo asincrono.
    loadVoices();
    if (typeof synth.onvoiceschanged !== "undefined") {
        synth.onvoiceschanged = loadVoices;
    }

    // ---- Evidenziazione ------------------------------------------------

    // Inietta la regola CSS dell'evidenziazione dentro il documento della
    // sezione (che vive in un iframe separato: reader.css non lo raggiunge).
    function injectHighlightStyle(doc) {
        if (!doc || doc.getElementById("tts-style")) {
            return;
        }
        var style = doc.createElement("style");
        style.id = "tts-style";
        style.textContent =
            "." +
            HIGHLIGHT_CLASS +
            "{background-color:rgba(43,91,140,.20);" +
            "box-shadow:0 0 0 2px rgba(43,91,140,.20);" +
            "border-radius:3px;transition:background-color .2s ease;}";
        (doc.head || doc.documentElement).appendChild(style);
    }

    function clearHighlight() {
        if (highlighted) {
            try {
                highlighted.classList.remove(HIGHLIGHT_CLASS);
            } catch (e) { /* noop */ }
            highlighted = null;
        }
    }

    function highlight(el) {
        clearHighlight();
        try {
            el.classList.add(HIGHLIGHT_CLASS);
            highlighted = el;
        } catch (e) { /* noop */ }
    }

    // ---- Coda di lettura ----------------------------------------------

    function currentContents() {
        var contents = reader.rendition.getContents();
        return contents && contents.length ? contents[0] : null;
    }

    // Indice del blocco che contiene la posizione di lettura corrente, così
    // "leggi" riparte da dove si è, non dall'inizio della sezione.
    function startIndexFromLocation(contents) {
        try {
            var loc = reader.rendition.currentLocation();
            var cfi = loc && loc.start && loc.start.cfi;
            if (!cfi || typeof contents.range !== "function") {
                return 0;
            }
            var range = contents.range(cfi);
            if (!range) {
                return 0;
            }
            var node = range.startContainer;
            var elt = node.nodeType === 1 ? node : node.parentElement;
            var block =
                elt && elt.closest ? elt.closest(BLOCK_SELECTOR) : null;
            if (!block) {
                return 0;
            }
            for (var i = 0; i < queue.length; i++) {
                if (
                    queue[i].el === block ||
                    queue[i].el.contains(block) ||
                    block.contains(queue[i].el)
                ) {
                    return i;
                }
            }
        } catch (e) { /* noop */ }
        return 0;
    }

    function buildQueue(contents) {
        queue = [];
        qi = 0;
        if (!contents || !contents.document) {
            return;
        }
        var doc = contents.document;
        injectHighlightStyle(doc);
        var root = doc.body || doc;
        var nodes = root.querySelectorAll(BLOCK_SELECTOR);
        Array.prototype.forEach.call(nodes, function (el) {
            var text = (el.textContent || "").replace(/\s+/g, " ").trim();
            // Solo blocchi "foglia": se il blocco ne contiene un altro dello
            // stesso tipo, il testo verrebbe letto due volte.
            if (text && !el.querySelector(BLOCK_SELECTOR)) {
                queue.push({ el: el, text: text });
            }
        });
    }

    // ---- Motore di lettura --------------------------------------------

    function speakCurrent() {
        if (state !== "playing") {
            return;
        }
        if (qi >= queue.length) {
            advanceSection();
            return;
        }
        var item = queue[qi];
        highlight(item.el);
        scrollToElement(item.el);

        var mySeq = ++speakSeq; // questa e' ora l'utterance attiva
        var utter = new window.SpeechSynthesisUtterance(item.text);
        utter.rate = rate;
        if (selectedVoice) {
            try {
                utter.voice = selectedVoice;
                utter.lang = selectedVoice.lang;
            } catch (e) { /* voce non valida: usa la default */ }
        }
        var advance = function () {
            // Avanza solo se siamo ancora in riproduzione e questa e'
            // l'ultima utterance emessa (le altre sono state annullate da
            // pausa/seek e vanno ignorate).
            if (state === "playing" && mySeq === speakSeq) {
                qi += 1;
                speakCurrent();
            }
        };
        utter.onend = advance;
        utter.onerror = advance;
        try {
            synth.cancel(); // evita accodamenti su alcuni browser
        } catch (e) { /* noop */ }
        synth.speak(utter);
    }

    // Porta la pagina visibile sul blocco in lettura (il reader è paginato
    // a colonne: senza questo l'utente sentirebbe testo fuori schermo).
    function scrollToElement(el) {
        var contents = currentContents();
        if (!contents || typeof contents.cfiFromNode !== "function") {
            return;
        }
        try {
            var cfi = contents.cfiFromNode(el);
            if (cfi) {
                navByTts = true;
                reader.rendition.display(cfi);
            }
        } catch (e) { /* noop */ }
    }

    function advanceSection() {
        var index = null;
        try {
            var loc = reader.rendition.currentLocation();
            if (loc && loc.start && typeof loc.start.index === "number") {
                index = loc.start.index;
            }
        } catch (e) { /* noop */ }

        var items = null;
        try {
            items = reader.book.spine.spineItems;
        } catch (e) { /* noop */ }

        if (index === null || !items || index + 1 >= items.length) {
            stop();
            return;
        }

        var nextHref = items[index + 1].href;
        navByTts = true;
        reader.rendition.display(nextHref).then(function () {
            // Piccola attesa: il contenuto della nuova sezione dev'essere pronto.
            setTimeout(function () {
                buildQueue(currentContents());
                speakCurrent();
            }, 60);
        });
    }

    // ---- Controlli -----------------------------------------------------

    function updateButton() {
        if (!toggleBtn) {
            return;
        }
        var playing = state === "playing";
        toggleBtn.classList.toggle("tts-playing", playing);
        toggleBtn.setAttribute(
            "aria-label",
            playing ? toggleBtn.dataset.labelPause : toggleBtn.dataset.labelPlay
        );
        toggleBtn.setAttribute("aria-pressed", playing ? "true" : "false");
    }

    function cancelSpeech() {
        speakSeq += 1; // invalida l'onend dell'utterance in corso
        try {
            synth.cancel();
        } catch (e) { /* noop */ }
    }

    function play() {
        // Ripresa dopo pausa: riparte dal blocco corrente (i blocchi sono le
        // "tracce"; non riprendiamo a meta' frase, e' piu' prevedibile).
        if (state === "paused") {
            state = "playing";
            updateButton();
            speakCurrent();
            return;
        }
        var contents = currentContents();
        if (!contents) {
            return;
        }
        buildQueue(contents);
        if (!queue.length) {
            return;
        }
        qi = startIndexFromLocation(contents);
        state = "playing";
        updateButton();
        speakCurrent();
    }

    function pause() {
        if (state !== "playing") {
            return;
        }
        state = "paused";
        cancelSpeech();
        updateButton();
    }

    function stop() {
        state = "stopped";
        cancelSpeech();
        clearHighlight();
        queue = [];
        qi = 0;
        updateButton();
    }

    // Sposta l'evidenziazione (e, se in riproduzione, la voce) sul blocco a
    // indice qi, senza doppioni con l'onend annullato.
    function seekTo(newIndex) {
        cancelSpeech();
        qi = newIndex;
        if (state === "playing") {
            speakCurrent();
        } else if (queue[qi]) {
            highlight(queue[qi].el);
            scrollToElement(queue[qi].el);
        }
    }

    function next() {
        if (state === "stopped" || !queue.length) {
            return;
        }
        if (qi + 1 >= queue.length) {
            // Fine sezione: se in riproduzione, passa al capitolo successivo.
            if (state === "playing") {
                cancelSpeech();
                advanceSection();
            }
            return;
        }
        seekTo(qi + 1);
    }

    function prev() {
        if (state === "stopped" || !queue.length) {
            return;
        }
        seekTo(qi > 0 ? qi - 1 : 0);
    }

    function toggle() {
        if (state === "playing") {
            pause();
        } else {
            play();
        }
    }

    if (toggleBtn) {
        toggleBtn.addEventListener("click", function (evt) {
            evt.preventDefault();
            toggle();
        });
    }
    if (prevBtn) {
        prevBtn.addEventListener("click", function (evt) {
            evt.preventDefault();
            prev();
        });
    }
    if (nextBtn) {
        nextBtn.addEventListener("click", function (evt) {
            evt.preventDefault();
            next();
        });
    }

    var stopBtn = document.getElementById("tts-stop");
    if (stopBtn) {
        stopBtn.addEventListener("click", function (evt) {
            evt.preventDefault();
            stop();
        });
    }

    var voiceSel = document.getElementById("tts-voice");
    if (voiceSel) {
        voiceSel.addEventListener("change", function () {
            for (var i = 0; i < voices.length; i++) {
                if (voices[i].voiceURI === voiceSel.value) {
                    selectedVoice = voices[i];
                    break;
                }
            }
            localStorage.setItem(VOICE_KEY, voiceSel.value);
        });
    }

    var rateInput = document.getElementById("tts-rate");
    var rateDisplay = document.getElementById("tts-rate-display");
    if (rateInput) {
        rateInput.value = rate;
        if (rateDisplay) {
            rateDisplay.textContent = rate.toFixed(1) + "×";
        }
        rateInput.addEventListener("input", function () {
            rate = parseFloat(rateInput.value) || 1;
            if (rateDisplay) {
                rateDisplay.textContent = rate.toFixed(1) + "×";
            }
            localStorage.setItem(RATE_KEY, String(rate));
            // Applica la nuova velocità al blocco successivo: la Web Speech API
            // non cambia il rate di un'utterance già in corso.
            if (state === "playing") {
                synth.cancel();
                speakCurrent();
            }
        });
    }

    // Se l'utente cambia pagina/sezione a mano mentre legge, fermiamo il TTS
    // per non desincronizzare voce e testo a schermo.
    reader.rendition.on("relocated", function () {
        if (navByTts) {
            navByTts = false;
            return;
        }
        if (state !== "stopped") {
            stop();
        }
    });

    // Alcuni browser continuano la sintesi dopo aver lasciato la pagina:
    // interrompiamo esplicitamente alla chiusura.
    window.addEventListener("beforeunload", function () {
        try {
            synth.cancel();
        } catch (e) { /* noop */ }
    });

    updateButton();
})();
