/* Board item descriptions, drawn as markdown with mermaid diagrams.

   A description is written by people and by minds, so nothing in it is
   trusted. Raw HTML in the source is shown as the text it is, never parsed
   (a description about `/api/kanban/<kind>/<id>` must read as that), and
   what marked produces is sanitised again before it reaches the page, with
   form controls, styles, ids and names stripped so a description cannot add
   a field to the form it sits beside or capture a lookup the page makes.

   The libraries come from pinned CDN URLs, as lucide and cytoscape do, and
   load without blocking: the source text is on the page from the first
   paint and is swapped for the rendering only once it is ready. Mermaid is
   fetched only when a description holds a diagram. A library that fails or
   hangs leaves the source text showing, never a blank. A diagram mermaid
   cannot draw stays as its source code block; the rest still renders. */
(function () {
    'use strict';

    const LIBRARIES = {
        marked: ['https://unpkg.com/marked@15.0.12/marked.min.js', () => window.marked],
        purify: ['https://unpkg.com/dompurify@3.2.6/dist/purify.min.js', () => window.DOMPurify],
        mermaid: ['https://unpkg.com/mermaid@11.6.0/dist/mermaid.min.js', () => window.mermaid],
    };
    const LOAD_TIMEOUT_MS = 15000;
    // Mermaid draws a "maximum text size exceeded" picture past this rather
    // than failing, so a block this long is treated as one it cannot draw.
    const MERMAID_MAX_TEXT = 50000;
    const loading = {};

    function load(name) {
        const [url, present] = LIBRARIES[name];
        if (present()) return Promise.resolve(present());
        if (!loading[name]) {
            loading[name] = new Promise((resolve, reject) => {
                const script = document.createElement('script');
                const timer = setTimeout(() => reject(new Error(`${name} timed out`)), LOAD_TIMEOUT_MS);
                script.src = url;
                script.async = true;
                script.onload = () => { clearTimeout(timer); present() ? resolve(present()) : reject(new Error(`${name} missing`)); };
                script.onerror = () => { clearTimeout(timer); reject(new Error(`${name} failed to load`)); };
                document.head.append(script);
            }).catch((err) => { delete loading[name]; throw err; });
        }
        return loading[name];
    }

    function escapeHtml(text) {
        return String(text).replace(/[&<>"']/g, (c) => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
    }

    let parser = null;
    let purifier = null;
    function tools() {
        if (!parser) {
            parser = new window.marked.Marked({
                gfm: true,
                // A plain-text description keeps the line breaks it was typed with.
                breaks: true,
                renderer: {
                    html(token) {
                        const text = escapeHtml(token.raw ?? token.text ?? '');
                        return token.block ? `<p>${text.replace(/\n+$/, '').replace(/\n/g, '<br>')}</p>` : text;
                    },
                    // An image is a link to it, never fetched by opening the item.
                    image(token) {
                        const label = token.text || token.href;
                        return `<a href="${escapeHtml(token.href)}">${escapeHtml(label)}</a>`;
                    },
                },
            });
        }
        if (!purifier) {
            purifier = window.DOMPurify(window);
            purifier.addHook('afterSanitizeAttributes', (node) => {
                if (node.tagName === 'A' && node.hasAttribute('href')) {
                    node.setAttribute('target', '_blank');
                    node.setAttribute('rel', 'noopener noreferrer');
                }
            });
        }
        return {parser, purifier};
    }

    const HTML_RULES = {
        USE_PROFILES: {html: true},
        FORBID_TAGS: ['style', 'form', 'input', 'textarea', 'select', 'option', 'button', 'img', 'video', 'audio', 'source', 'picture'],
        // `class` stays: marked marks a fenced block with its language there.
        FORBID_ATTR: ['style', 'id', 'name'],
    };

    function showSource(target, source) {
        const plain = document.createElement('div');
        plain.className = 'md-source';
        plain.textContent = source;
        target.replaceChildren(plain);
    }

    let mermaidReady = null;
    function getMermaid() {
        if (!mermaidReady) {
            mermaidReady = load('mermaid').then((mermaid) => {
                mermaid.initialize({
                    startOnLoad: false,
                    securityLevel: 'strict',
                    theme: 'dark',
                    suppressErrorRendering: true,
                    maxTextSize: MERMAID_MAX_TEXT,
                    // Labels as SVG text, not HTML in a foreignObject, so the
                    // drawing survives the SVG-only sanitising below.
                    htmlLabels: false,
                    flowchart: {htmlLabels: false},
                    // Keys a diagram's own %%{init}%% or front matter may not
                    // change: mermaid's defaults, plus the CSS a diagram could
                    // aim at the whole page and the label mode it could turn
                    // back into HTML.
                    secure: ['secure', 'securityLevel', 'startOnLoad', 'maxTextSize', 'suppressErrorRendering', 'maxEdges',
                        'themeCSS', 'fontFamily', 'altFontFamily', 'htmlLabels', 'flowchart'],
                });
                return mermaid;
            }).catch((err) => { mermaidReady = null; throw err; });
        }
        return mermaidReady;
    }

    /* The diagram's stylesheet, reduced to rules that only reach inside it:
       every selector starts at the diagram's own id and goes down from it,
       nothing escapes it through nesting, and nothing fetches. Anything else a
       diagram's text managed to put there is dropped. */
    function scopedCss(text, id) {
        const sheet = new CSSStyleSheet();
        try { sheet.replaceSync(text); } catch (_) { return ''; }
        const root = `#${id}`;
        // The diagram itself, or something beneath it: not a sibling, and
        // not a nested rule whose `&` could put the diagram anywhere.
        const inside = (selector) => {
            if (selector.includes('&') || !selector.startsWith(root)) return false;
            const rest = selector.slice(root.length);
            return rest === '' || rest.startsWith('.') || (/^\s/.test(rest) && !/^[~+]/.test(rest.trim()));
        };
        const kept = [];
        for (const rule of sheet.cssRules) {
            if (/url\(|@import|image-set/i.test(rule.cssText)) continue;
            if (rule instanceof CSSKeyframesRule) { kept.push(rule.cssText); continue; }
            if (!(rule instanceof CSSStyleRule) || (rule.cssRules && rule.cssRules.length)) continue;
            if (rule.selectorText.split(',').map((part) => part.trim()).every(inside)) kept.push(rule.cssText);
        }
        return kept.join('\n');
    }

    function sanitizeSvg(svg, id) {
        // An id the page already uses would capture the page's own lookups
        // (a gantt task may be called anything); the diagram's own prefixed
        // ids are the ones its markers and stylesheet need.
        const hook = (_node, data) => {
            if (data.attrName === 'id' && !data.attrValue.startsWith(id) && document.getElementById(data.attrValue)) data.keepAttr = false;
        };
        purifier.addHook('uponSanitizeAttribute', hook);
        let clean;
        try {
            clean = purifier.sanitize(svg, {
                USE_PROFILES: {svg: true, svgFilters: true},
                ADD_TAGS: ['style'],
                FORBID_TAGS: ['foreignObject', 'a', 'script'],
                FORBID_ATTR: ['href', 'xlink:href'],
                RETURN_DOM_FRAGMENT: true,
            });
        } finally {
            purifier.removeHook('uponSanitizeAttribute');
        }
        for (const style of clean.querySelectorAll('style')) style.textContent = scopedCss(style.textContent, id);
        return clean;
    }

    let diagramSeq = 0;
    let renderSeq = 0;
    async function drawDiagrams(target, token, blocks) {
        let mermaid;
        try { mermaid = await getMermaid(); } catch (_) { return; }
        for (const code of blocks) {
            if (target.dataset.mdToken !== token) return;
            const text = code.textContent;
            if (text.length > MERMAID_MAX_TEXT) continue;
            const id = `hive-mermaid-${++diagramSeq}`;
            try {
                await mermaid.parse(text);
                const {svg} = await mermaid.render(id, text);
                if (target.dataset.mdToken !== token) return;
                // HTML labels would be stripped to a drawing with no words in
                // it; a diagram that produced them is one we cannot draw.
                if (/<foreignObject/i.test(svg)) throw new Error('html labels');
                const figure = document.createElement('div');
                figure.className = 'md-diagram';
                figure.append(sanitizeSvg(svg, id));
                code.closest('pre').replaceWith(figure);
            } catch (_) {
                // Left as its source block; mermaid's scratch node goes too.
                document.getElementById(`d${id}`)?.remove();
                document.getElementById(id)?.remove();
            }
        }
    }

    /* Draw `source` into `target`. Safe to call again on the same target:
       a render overtaken by a newer one is dropped rather than landing. */
    async function render(target, source) {
        source = source == null ? '' : String(source);
        const token = String(++renderSeq);
        target.dataset.mdToken = token;
        target.classList.add('md-render');
        showSource(target, source);
        if (!source.trim()) return;
        try {
            await Promise.all([load('marked'), load('purify')]);
        } catch (_) {
            return;
        }
        if (target.dataset.mdToken !== token) return;
        let body;
        try {
            const {parser, purifier} = tools();
            body = document.createElement('div');
            body.className = 'md-body';
            body.innerHTML = purifier.sanitize(parser.parse(source), HTML_RULES);
        } catch (_) {
            return;
        }
        // Markdown that draws nothing (a lone link reference) keeps the source.
        if (!body.textContent.trim() && !body.querySelector('hr')) return;
        target.replaceChildren(body);
        const blocks = Array.from(body.querySelectorAll('pre > code.language-mermaid'));
        if (blocks.length) await drawDiagrams(target, token, blocks);
    }

    window.hiveMarkdown = {render};
})();
