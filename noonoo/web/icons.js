/* Hand-drawn XP-era icons as inline SVG. Each call gets unique gradient ids. */
(function () {
  let n = 0;
  const uid = () => "g" + (++n);
  const svg = (w, h, body, vb) =>
    `<svg width="${w}" height="${h}" viewBox="${vb || `0 0 ${w} ${h}`}" xmlns="http://www.w3.org/2000/svg">${body}</svg>`;
  const lin = (id, stops, x2 = 0, y2 = 1) =>
    `<linearGradient id="${id}" x1="0" y1="0" x2="${x2}" y2="${y2}">${stops
      .map(([o, c]) => `<stop offset="${o}" stop-color="${c}"/>`).join("")}</linearGradient>`;
  const rad = (id, stops, cx = 0.35, cy = 0.3) =>
    `<radialGradient id="${id}" cx="${cx}" cy="${cy}" r="0.75">${stops
      .map(([o, c]) => `<stop offset="${o}" stop-color="${c}"/>`).join("")}</radialGradient>`;

  // NooNoo: the Teletubbies' blue dome vacuum - big eyes on top, hose nose, bristle skirt.
  function noonoo(size) {
    const a = uid(), b = uid();
    return svg(size, size, `<defs>${rad(a, [[0, "#cfe4ff"], [0.45, "#4f8cf0"], [1, "#163f9a"]], 0.4, 0.3)}${lin(b, [[0, "#4a4f8a"], [1, "#1e2250"]])}</defs>
      <path d="M22 19.5 q6 0 7 -6.5" fill="none" stroke="#163f9a" stroke-width="3.6" stroke-linecap="round"/>
      <path d="M22 19.5 q6 0 7 -6.5" fill="none" stroke="#6fa4ff" stroke-width="1.6" stroke-linecap="round"/>
      <circle cx="29" cy="12.3" r="2.3" fill="#2a2f6a" stroke="#111a44" stroke-width=".8"/>
      <path d="M3.5 23 Q3.5 7.5 15 7.5 Q26.5 7.5 26.5 23 Z" fill="url(#${a})" stroke="#0f2f7a" stroke-width="1.1"/>
      <rect x="2.5" y="22" width="25" height="5.5" rx="2.6" fill="url(#${b})" stroke="#111a44" stroke-width=".9"/>
      <path d="M6 23.5v3M9 23.5v3M12 23.5v3M15 23.5v3M18 23.5v3M21 23.5v3M24 23.5v3" stroke="#7b80c0" stroke-width=".8"/>
      <g class="nn-eyes">
      <circle cx="11" cy="13.5" r="3.6" fill="#fff" stroke="#0f2f7a" stroke-width=".9"/>
      <circle cx="19" cy="13.5" r="3.6" fill="#fff" stroke="#0f2f7a" stroke-width=".9"/>
      <circle cx="11.6" cy="12.6" r="1.7" fill="#111"/><circle cx="19.6" cy="12.6" r="1.7" fill="#111"/>
      <circle cx="12.1" cy="12.1" r=".55" fill="#fff"/><circle cx="20.1" cy="12.1" r=".55" fill="#fff"/></g>
      <path d="M8.5 9.6 l-.9 -1.4 M11 9.2 v-1.6 M13.5 9.6 l.9 -1.4 M16.5 9.6 l-.9 -1.4 M19 9.2 v-1.6 M21.5 9.6 l.9 -1.4" stroke="#0f2f7a" stroke-width=".8" stroke-linecap="round"/>
      <ellipse cx="9" cy="17.5" rx="3" ry="1.3" fill="#fff" opacity=".3"/>`, "0 0 32 32");
  }

  const I = {
    app16: () => noonoo(16),
    logo: () => {
      return `<span class="logo-mark">${noonoo(32)}</span><span class="logo-text"><b>Noo</b>Noo</span>`;
    },
    logoBig: () => noonoo(72),

    search16: () => {
      const a = uid();
      return svg(16, 16, `<defs>${rad(a, [[0, "#ffffff"], [0.5, "#bfe0ff"], [1, "#4f8fd6"]])}</defs>
        <path d="M9.5 9.5 L14.5 14.5" stroke="#6b4a1e" stroke-width="3" stroke-linecap="round"/>
        <circle cx="6.5" cy="6.5" r="5" fill="url(#${a})" stroke="#2a5aa0" stroke-width="1.3"/>`);
    },
    search24: () => {
      const a = uid();
      return svg(24, 24, `<defs>${rad(a, [[0, "#ffffff"], [0.5, "#bfe0ff"], [1, "#3f7fd0"]])}</defs>
        <path d="M14 14 L21.5 21.5" stroke="#5a3a12" stroke-width="4.2" stroke-linecap="round"/>
        <path d="M14 14 L21 21" stroke="#b07a3a" stroke-width="2" stroke-linecap="round"/>
        <circle cx="9.5" cy="9.5" r="7.2" fill="url(#${a})" stroke="#1f4f96" stroke-width="1.8"/>
        <ellipse cx="7.5" cy="6.8" rx="3" ry="1.8" fill="#fff" opacity=".8"/>`);
    },
    monitor24: () => {
      const a = uid(), b = uid();
      return svg(24, 24, `<defs>${lin(a, [[0, "#f4f4f4"], [1, "#a9a9a9"]])}${lin(b, [[0, "#5fa0ff"], [1, "#0d3fa6"]], 1, 1)}</defs>
        <rect x="2" y="2.5" width="20" height="15" rx="2" fill="url(#${a})" stroke="#555"/>
        <rect x="4.2" y="4.5" width="15.6" height="11" fill="url(#${b})" stroke="#334"/>
        <path d="M5 12 L8 9 L10.5 11 L14 7 L19 11" stroke="#9cff6a" stroke-width="1.3" fill="none"/>
        <path d="M8 18 h8 l1.5 3.5 h-11 z" fill="url(#${a})" stroke="#555"/>`);
    },
    monitor16: () => {
      const b = uid();
      return svg(16, 16, `<defs>${lin(b, [[0, "#5fa0ff"], [1, "#0d3fa6"]], 1, 1)}</defs>
        <rect x="1" y="1.5" width="14" height="10" rx="1.5" fill="#ddd" stroke="#555"/>
        <rect x="2.5" y="3" width="11" height="7" fill="url(#${b})"/>
        <path d="M3 8 L5.5 6 L7.5 7.5 L10 4.5 L13 7" stroke="#9cff6a" fill="none"/>
        <path d="M5 12.5 h6 l1 2.5 h-8z" fill="#ccc" stroke="#555"/>`);
    },
    library24: () => folder(24),
    library16: () => folder(16),
    folder16: () => folder(16),
    globe16: () => globe(16),

    download32: () => {
      const a = uid();
      return svg(32, 32, `<defs>${lin(a, [[0, "#b7e86b"], [1, "#3f9a12"]])}</defs>
        <path d="M11 3 h10 v12 h7 L16 28 L4 15 h7 z" fill="url(#${a})" stroke="#2b6a0b" stroke-width="1.5" stroke-linejoin="round"/>
        <path d="M13 5 h3 v11 h-3z" fill="#fff" opacity=".35"/>`);
    },
    audio32: () => {
      const a = uid();
      return svg(32, 32, `<defs>${lin(a, [[0, "#9fd0ff"], [1, "#1f5fc0"]])}</defs>
        <path d="M12 6 L26 3 V21" fill="none" stroke="#183f7a" stroke-width="3"/>
        <path d="M12 6 V24" stroke="#183f7a" stroke-width="3"/>
        <ellipse cx="8.5" cy="24.5" rx="5" ry="4" fill="url(#${a})" stroke="#183f7a" stroke-width="1.4"/>
        <ellipse cx="22.5" cy="21.5" rx="5" ry="4" fill="url(#${a})" stroke="#183f7a" stroke-width="1.4"/>
        <path d="M12 6 L26 3 V8 L12 11 z" fill="#183f7a"/>`);
    },
    browse32: () => globe(32),
    stop32: () => {
      const a = uid();
      return svg(32, 32, `<defs>${rad(a, [[0, "#ff9a8a"], [0.6, "#e0301a"], [1, "#9a1206"]])}</defs>
        <circle cx="16" cy="16" r="13" fill="url(#${a})" stroke="#7a0d04" stroke-width="1.4"/>
        <path d="M10.5 10.5 L21.5 21.5 M21.5 10.5 L10.5 21.5" stroke="#fff" stroke-width="3.2" stroke-linecap="round"/>
        <ellipse cx="12" cy="8.5" rx="6" ry="3" fill="#fff" opacity=".35"/>`);
    },
    junk32: () => {
      const a = uid(), b = uid();
      return svg(32, 32, `<defs>${lin(a, [[0, "#f2f2f2"], [0.5, "#c9c9c9"], [1, "#8a8a8a"]], 1, 0)}${lin(b, [[0, "#eee"], [1, "#999"]])}</defs>
        <path d="M7 9 h18 l-2 20 h-14 z" fill="url(#${a})" stroke="#555" stroke-width="1.3"/>
        <path d="M11.5 12 l.7 14 M16 12 v14 M20.5 12 l-.7 14" stroke="#666" stroke-width="1.2"/>
        <rect x="5" y="5.5" width="22" height="4" rx="1.5" fill="url(#${b})" stroke="#555" stroke-width="1.2"/>
        <rect x="13" y="3" width="6" height="3" rx="1" fill="none" stroke="#555" stroke-width="1.2"/>`);
    },
    pause32: () => {
      const a = uid();
      return svg(32, 32, `<defs>${lin(a, [[0, "#ffe58a"], [1, "#d99a00"]], 1, 0)}</defs>
        <rect x="7" y="5" width="7" height="22" rx="1.5" fill="url(#${a})" stroke="#8a5c00" stroke-width="1.3"/>
        <rect x="18" y="5" width="7" height="22" rx="1.5" fill="url(#${a})" stroke="#8a5c00" stroke-width="1.3"/>`);
    },
    resume32: () => playTri(32, "#b7e86b", "#3f9a12", "#2b6a0b"),
    play32: () => playTri(32, "#9fd0ff", "#1f5fc0", "#183f7a"),
    launch32: () => {
      const a = uid();
      return svg(32, 32, `<defs>${lin(a, [[0, "#ffffff"], [1, "#c8d8f0"]])}</defs>
        <rect x="3" y="5" width="22" height="20" rx="2" fill="url(#${a})" stroke="#335" stroke-width="1.3"/>
        <rect x="3" y="5" width="22" height="5" rx="2" fill="#2b63d8"/>
        <path d="M17 14 h11 v11" fill="none" stroke="#2b6a0b" stroke-width="2.5"/>
        <path d="M28 14 L16 26" stroke="#2b6a0b" stroke-width="2.5"/>`);
    },
    explore32: () => {
      const a = uid(), b = uid();
      return svg(32, 32, `<defs>${lin(a, [[0, "#ffe9a0"], [1, "#e0a92a"]])}${rad(b, [[0, "#fff"], [1, "#8fc0ff"]])}</defs>
        <path d="M2 8 h9 l3 3 h14 v16 h-26z" fill="url(#${a})" stroke="#8a6510" stroke-width="1.3"/>
        <circle cx="19" cy="18" r="5.5" fill="url(#${b})" stroke="#1f4f96" stroke-width="1.6"/>
        <path d="M23 22 l5 5" stroke="#5a3a12" stroke-width="3" stroke-linecap="round"/>`);
    },
    // circular arrow: a 300° arc with the arrowhead closing the gap at the top
    retry32: () => {
      const a = uid();
      const arc = "M24.66 11 A10 10 0 1 1 14.6 6.1";
      return svg(32, 32, `<defs>${lin(a, [[0, "#b7e86b"], [1, "#3f9a12"]])}</defs>
        <path d="${arc}" fill="none" stroke="#1f6a12" stroke-width="5.2" stroke-linecap="round"/>
        <path d="${arc}" fill="none" stroke="url(#${a})" stroke-width="2.8" stroke-linecap="round"/>
        <path d="M13.6 0.9 L22.8 6.1 L13.6 11.3 Z" fill="url(#${a})" stroke="#1f6a12" stroke-width="1.3" stroke-linejoin="round"/>`);
    },
    clear32: () => {
      const a = uid();
      return svg(32, 32, `<defs>${lin(a, [[0, "#ffe08a"], [1, "#c88a10"]], 1, 0)}</defs>
        <path d="M24 3 L15 17" stroke="#6b4a1e" stroke-width="3" stroke-linecap="round"/>
        <path d="M11 15 L19 20 L14 29 L3 23 z" fill="url(#${a})" stroke="#7a5410" stroke-width="1.3" stroke-linejoin="round"/>
        <path d="M7 22 L5 26 M10 23.5 L8 27.5 M13 25 L11 29" stroke="#7a5410" stroke-width="1"/>`);
    },

    fileAudio: () => svg(16, 16, `
        <path d="M2.5 1 h7.5 l3.5 3.5 v10.5 h-11z" fill="#fff" stroke="#7a7a7a"/>
        <path d="M10 1 v3.5 h3.5" fill="#e8e8e8" stroke="#7a7a7a"/>
        <path d="M4.5 7.5 h2 l2.5 -2 v7 l-2.5 -2 h-2z" fill="#3a6fd0" stroke="#1f3f80" stroke-width=".7"/>
        <path d="M10.5 7 q1.3 1.5 0 3" stroke="#3a6fd0" fill="none"/>`),
    fileVideo: () => svg(16, 16, `
        <rect x="1" y="2.5" width="14" height="11" rx="1" fill="#333" stroke="#111"/>
        <rect x="3.5" y="4" width="9" height="8" fill="#6aa8ff"/>
        <path d="M2 4h1M2 6h1M2 8h1M2 10h1M2 12h1M13 4h1M13 6h1M13 8h1M13 10h1M13 12h1" stroke="#ddd"/>
        <path d="M6.5 6 L10 8 L6.5 10z" fill="#fff"/>`),
    fileOther: () => svg(16, 16, `
        <path d="M2.5 1 h7.5 l3.5 3.5 v10.5 h-11z" fill="#fff" stroke="#7a7a7a"/>
        <path d="M10 1 v3.5 h3.5" fill="#e8e8e8" stroke="#7a7a7a"/>
        <path d="M4.5 7h7M4.5 9h7M4.5 11h5" stroke="#aaa"/>`),
    playlist16: () => svg(16, 16, `
        <rect x="1.5" y="2" width="13" height="12" fill="#fff" stroke="#7a7a7a"/>
        <path d="M3.5 5h7M3.5 8h7M3.5 11h5" stroke="#3a6fd0"/><path d="M11.5 9.5 l2.5 1.5 l-2.5 1.5z" fill="#3a9a12"/>`),
    user16: () => svg(16, 16, `
        <circle cx="8" cy="5" r="3.2" fill="#f2c79a" stroke="#8a5a2a"/>
        <path d="M2 15 q0 -6 6 -6 q6 0 6 6z" fill="#4a86e0" stroke="#1f3f80"/>`),
    clock16: () => svg(16, 16, `
        <circle cx="8" cy="8" r="6.5" fill="#fff" stroke="#2a5aa0" stroke-width="1.3"/>
        <path d="M8 4 V8 L11 9.5" stroke="#222" stroke-width="1.2" fill="none"/>`),
    all16: () => svg(16, 16, `
        <rect x="1.5" y="1.5" width="6" height="6" fill="#e0a92a" stroke="#8a6510"/><rect x="8.5" y="1.5" width="6" height="6" fill="#4a86e0" stroke="#1f3f80"/>
        <rect x="1.5" y="8.5" width="6" height="6" fill="#4fb81e" stroke="#2b6a0b"/><rect x="8.5" y="8.5" width="6" height="6" fill="#e0301a" stroke="#7a0d04"/>`),

    signal: () => svg(26, 16, [0, 1, 2, 3, 4].map(i =>
      `<rect x="${1 + i * 5}" y="${12 - i * 2.6}" width="3.6" height="${3 + i * 2.6}" class="sig sig${i}" stroke="#1b5e0a" stroke-width=".6"/>`).join("")),

    star: (on) => svg(12, 12, `<path d="M6 .8 L7.6 4.3 L11.3 4.6 L8.5 7.1 L9.4 10.9 L6 8.9 L2.6 10.9 L3.5 7.1 L.7 4.6 L4.4 4.3z"
        fill="${on ? "#ffcc33" : "#e6e6e6"}" stroke="${on ? "#b07a00" : "#b8b8b8"}" stroke-width=".8"/>`),
    xRed: () => svg(13, 13, `<rect x=".5" y=".5" width="12" height="12" rx="2.5" fill="#e0482a" stroke="#8a1a06"/>
        <path d="M4 4 L9 9 M9 4 L4 9" stroke="#fff" stroke-width="1.8" stroke-linecap="round"/>`),
    info32: () => {
      const a = uid();
      return svg(32, 32, `<defs>${rad(a, [[0, "#ffffff"], [0.5, "#8fc0ff"], [1, "#1f5fc0"]])}</defs>
        <circle cx="16" cy="16" r="14" fill="url(#${a})" stroke="#183f7a" stroke-width="1.3"/>
        <circle cx="16" cy="9" r="2.2" fill="#fff"/><rect x="14" y="13" width="4" height="12" rx="1.5" fill="#fff"/>`);
    },
    warn32: () => {
      const a = uid();
      return svg(32, 32, `<defs>${lin(a, [[0, "#fff19a"], [1, "#f0b400"]])}</defs>
        <path d="M16 2.5 L30 28.5 H2z" fill="url(#${a})" stroke="#8a6500" stroke-width="1.3" stroke-linejoin="round"/>
        <rect x="14.5" y="10" width="3" height="10" rx="1" fill="#222"/><circle cx="16" cy="24" r="1.8" fill="#222"/>`);
    },
  };

  function folder(s) {
    const a = uid(), b = uid();
    return svg(s, s, `<defs>${lin(a, [[0, "#ffe9a0"], [1, "#e0a92a"]])}${lin(b, [[0, "#fff6d0"], [1, "#f0c050"]])}</defs>
      <path d="M1.5 5 h7 l2 2 h12 v14.5 h-21z" fill="url(#${a})" stroke="#8a6510" stroke-width="1"/>
      <path d="M1.5 9 h21 v12.5 h-21z" fill="url(#${b})" stroke="#8a6510" stroke-width="1"/>
      <path d="M5 13 h10" stroke="#fff" stroke-width="1.2" opacity=".7"/>`, "0 0 24 24");
  }
  function globe(s) {
    const a = uid();
    return svg(s, s, `<defs>${rad(a, [[0, "#c8f0ff"], [0.55, "#3f9ae0"], [1, "#10508f"]])}</defs>
      <circle cx="16" cy="16" r="13.5" fill="url(#${a})" stroke="#0c3a6a" stroke-width="1.3"/>
      <path d="M8 8 q4 1 4 5 q-3 2 -1 5 q2 2 0 6 q-4 -2 -6 -7 q0 -5 3 -9z M18 5 q5 1 7 6 q-3 0 -5 3 q-3 -1 -4 -4 q0 -3 2 -5z M19 18 q4 -1 6 2 q-2 5 -6 6 q-2 -3 0 -8z"
        fill="#5fc030" stroke="#2b6a0b" stroke-width=".7"/>
      <ellipse cx="12" cy="8.5" rx="6" ry="3" fill="#fff" opacity=".35"/>`, "0 0 32 32");
  }
  function playTri(s, hi, lo, line) {
    const a = uid();
    return svg(s, s, `<defs>${lin(a, [[0, `${hi}`], [1, `${lo}`]])}</defs>
      <path d="M8 4 L27 16 L8 28z" fill="url(#${a})" stroke="${line}" stroke-width="1.5" stroke-linejoin="round"/>
      <path d="M10 8 L20 14.5 L10 14.5z" fill="#fff" opacity=".35"/>`, "0 0 32 32");
  }

  window.ICONS = I;
  window.icon = (name, ...a) => (I[name] ? I[name](...a) : "");
  window.paintIcons = (root = document) =>
    root.querySelectorAll("[data-icon]").forEach((el) => {
      if (!el.dataset.painted) {
        el.insertAdjacentHTML("afterbegin", icon(el.dataset.icon));
        el.dataset.painted = 1;
      }
    });
})();
