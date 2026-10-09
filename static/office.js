// Pixel-art office for KAI (the Auditor) and JAX (the Fixer), drawn with plain canvas
// calls. No game engine and no sprite files: every sprite is a string grid
// below, so the page stays offline and dependency-free.
//
// app.js feeds it the same websocket events it puts in the activity log;
// this file only turns them into where each agent walks and what they say.
"use strict";

(() => {
  const W = 480;
  const H = 288;
  const CORRIDOR_Y = 190;
  const WALK_SPEED = 75; // logical px per second
  const BUBBLE_MS = 12000;

  const C = {
    bg: "#1a1a2e",
    floorA: "#2a2a3c",
    floorB: "#2e2e42",
    wall: "#20203a",
    wallTrim: "#3a3a60",
    wood: "#6d4c2e",
    woodLight: "#8a6440",
    woodDark: "#4b331f",
    metal: "#7f8c8d",
    metalDark: "#2d3436",
    screenOff: "#161c22",
    chair: "#44446a",
    paper: "#eef0f4",
    paperEdge: "#aab4be",
    auditor: "#8c7cf6",
    auditorDark: "#2b2463",
    fixer: "#2ec4a0",
    fixerDark: "#0f3d33",
    led: "#55efc4",
    ledBusy: "#fdcb6e",
    ledOff: "#33404a",
    red: "#d63031",
    ink: "#1a1a2e",
  };

  const SPOTS = {
    auditorSeat: { x: 90, y: 128, face: "up" },
    auditorIdle: { x: 152, y: 166, face: "down" },
    fixerSeat: { x: 390, y: 128, face: "up" },
    fixerIdle: { x: 328, y: 166, face: "down" },
    tableLeft: { x: 198, y: 160, face: "down" },
    tableRight: { x: 282, y: 160, face: "down" },
    rack: { x: 240, y: 278, face: "up" },
    printer: { x: 50, y: 276, face: "up" },
  };

  // ---------- sprites: 10 x 14 grids, one char per pixel ----------

  const HEADS = {
    auditor: {
      down: ["...HHHH...", "..HHHHHH..", "..HSSSSH..", "..GEGGEG..", "..SSSSSS..", "...SSSS..."],
      up: ["...HHHH...", "..HHHHHH..", "..HHHHHH..", "..HHHHHH..", "..SHHHHS..", "...SSSS..."],
    },
    fixer: {
      down: ["...YYYY...", "..YYYYYY..", ".YYYYYYYY.", "..SESSES..", "..SSSSSS..", "...SSSS..."],
      up: ["...YYYY...", "..YYYYYY..", ".YYYYYYYY.", "..HHHHHH..", "..SHHHHS..", "...SSSS..."],
    },
  };
  const TORSO = ["..TTTTTT..", ".TTTTTTTT.", ".STTTTTTS.", ".STtTTtTS.", "..WWWWWW.."];
  const LEGS = {
    stand: ["..PP..PP..", "..PP..PP..", "..BB..BB.."],
    stepA: ["..PP..PP..", "..PP..BB..", "..BB......"],
    stepB: ["..PP..PP..", "..BB..PP..", "......BB.."],
  };
  const COLORS = {
    auditor: { H: "#3b2a20", S: "#f1c27d", E: "#15151f", G: "#d6dee8", T: "#7c6cf0", t: "#5847c9", W: "#2f2f46", P: "#2f2f46", B: "#15151f" },
    fixer: { H: "#2d1f17", Y: "#fdcb6e", S: "#d9a066", E: "#15151f", T: "#25b591", t: "#178a6d", W: "#8b5a2b", P: "#3a4a5a", B: "#15151f" },
  };
  const PX = 2; // logical px per sprite pixel
  const SPRITE_W = 10;
  const SPRITE_H = 14;

  // ---------- state ----------

  let view = null; // the visible canvas
  let viewCtx = null;
  let buffer = null; // the scene, drawn at a whole-number scale
  let ctx = null; // what the drawing helpers draw into
  let renderScale = 1;
  let instant = false;
  let last = 0;
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)");

  const scene = {
    board: { mode: null, pass: 0, max: 0, blocking: null, fixed: 0 },
    table: 0,
    handedOver: false,
    monitors: { auditor: false, fixer: false },
    rackBusyUntil: 0,
    printStartedAt: -1,
  };

  function makeAgent(who, spot) {
    return {
      who,
      x: spot.x,
      y: spot.y,
      face: spot.face,
      dest: spot,
      path: [],
      queue: [],
      waitUntil: 0,
      working: false,
      carrying: 0,
      bubble: null,
      emote: null,
      bubbleAt: 0,
    };
  }

  const agents = {
    auditor: makeAgent("auditor", SPOTS.auditorIdle),
    fixer: makeAgent("fixer", SPOTS.fixerIdle),
  };

  // ---------- movement ----------

  // Desks sit in the top half, so walks go down to the corridor, across,
  // then up: nobody walks through furniture.
  function route(from, to) {
    if (Math.abs(from.y - to.y) < 4) return [to];
    const points = [{ x: from.x, y: CORRIDOR_Y }, { x: to.x, y: CORRIDOR_Y }, to];
    return points.filter((p, i) => i === points.length - 1 || Math.hypot(p.x - from.x, p.y - from.y) > 1);
  }

  const go = (a, spot) => a.queue.push({ go: SPOTS[spot] });
  const then = (a, fn) => a.queue.push({ call: fn });
  const pause = (a, ms) => a.queue.push({ wait: ms });

  function say(a, text, emote) {
    a.bubble = text;
    a.emote = emote || null;
    a.bubbleAt = performance.now();
  }

  function walk(a, dt) {
    const target = a.path[0];
    const dx = target.x - a.x;
    const dy = target.y - a.y;
    const dist = Math.hypot(dx, dy);
    const stride = WALK_SPEED * dt;
    if (dist <= stride) {
      a.x = target.x;
      a.y = target.y;
      a.path.shift();
      if (!a.path.length) a.face = a.dest.face;
      return;
    }
    a.x += (dx / dist) * stride;
    a.y += (dy / dist) * stride;
    a.face = dy < -0.5 ? "up" : "down";
  }

  function tick(a, dt, now) {
    for (let guard = 0; guard < 64; guard++) {
      // Skip the walking when events arrive faster than anyone can walk
      // (or when replaying a finished run), so the scene never lags behind.
      const walksQueued = a.queue.filter((step) => step.go).length;
      const fast = instant || reduced.matches || walksQueued > 2;
      if (a.path.length) {
        if (!fast) { walk(a, dt); return; }
        const end = a.path[a.path.length - 1];
        a.x = end.x;
        a.y = end.y;
        a.path.length = 0;
        a.face = a.dest.face;
        continue;
      }
      if (!fast && now < a.waitUntil) return;
      const step = a.queue[0];
      if (!step) return;
      if (step.until && !fast && !step.until()) {
        step.deadline = step.deadline || now + 6000;
        if (now < step.deadline) return;
      }
      a.queue.shift();
      if (step.go) {
        a.dest = step.go;
        a.path = route(a, step.go);
      } else if (step.wait) {
        a.waitUntil = fast ? 0 : now + step.wait;
      } else if (step.call) {
        step.call();
      }
    }
  }

  // ---------- drawing helpers ----------

  function rect(x, y, w, h, color) {
    ctx.fillStyle = color;
    ctx.fillRect(x, y, w, h);
  }

  function outline(x, y, w, h, color) {
    rect(x, y, w, 1, color);
    rect(x, y + h - 1, w, 1, color);
    rect(x, y, 1, h, color);
    rect(x + w - 1, y, 1, h, color);
  }

  function grid(rows, colors, x, y, px) {
    for (let r = 0; r < rows.length; r++) {
      const row = rows[r];
      for (let c = 0; c < row.length; c++) {
        const ch = row[c];
        if (ch === ".") continue;
        ctx.fillStyle = colors[ch];
        ctx.fillRect(x + c * px, y + r * px, px, px);
      }
    }
  }

  function text(str, x, y, color, size = 7, align = "left", weight = "") {
    ctx.font = `${weight} ${size}px ui-monospace, "SF Mono", Menlo, Consolas, monospace`.trim();
    ctx.fillStyle = color;
    ctx.textAlign = align;
    ctx.textBaseline = "top";
    ctx.fillText(str, x, y);
  }

  // ---------- furniture ----------

  function windowPane(x, y, w, h) {
    rect(x - 2, y - 2, w + 4, h + 4, C.metalDark);
    rect(x, y, w, h, "#24365e");
    rect(x + 4, y + 3, 10, 2, "#3d5a8a");
    rect(x + w - 22, y + h - 7, 14, 2, "#3d5a8a");
    rect(x + w / 2 - 1, y, 2, h, C.metalDark);
  }

  function whiteboard(x, y, w, h) {
    const b = scene.board;
    rect(x - 2, y - 2, w + 4, h + 4, "#95a5a6");
    rect(x, y, w, h, "#e9edf1");
    rect(x + 8, y + h, 20, 2, "#636e72");
    rect(x + 12, y + h, 4, 2, C.red);
    if (!b.mode) {
      text("ZweiGeeks", x + w / 2, y + 6, C.ink, 9, "center", "bold");
      text("waiting for a repo", x + w / 2, y + 18, "#636e72", 6, "center");
      return;
    }
    const head = b.mode === "audit" ? "Audit only"
      : b.pass > b.max ? "Final check" : `Pass ${b.pass || "-"} of ${b.max}`;
    text(head, x + 6, y + 5, C.ink, 8, "left", "bold");
    const blocking = b.blocking == null ? "-" : String(b.blocking);
    text("Blocking", x + 6, y + 17, "#636e72", 6);
    text(blocking, x + 44, y + 16, b.blocking ? C.red : "#0f7a66", 8, "left", "bold");
    if (b.mode !== "audit") {
      text("Fixed", x + 68, y + 17, "#636e72", 6);
      text(String(b.fixed), x + 92, y + 16, "#0f7a66", 8, "left", "bold");
    }
  }

  function desk(x, y, w) {
    rect(x, y, w, 20, C.wood);
    rect(x, y, w, 2, C.woodLight);
    rect(x, y + 20, w, 8, C.woodDark);
    rect(x + 2, y + 28, 3, 4, C.woodDark);
    rect(x + w - 5, y + 28, 3, 4, C.woodDark);
  }

  function monitor(x, y, on, glow, dark, now) {
    rect(x, y, 26, 18, C.metalDark);
    rect(x + 2, y + 2, 22, 13, on ? dark : C.screenOff);
    if (on) {
      const tickN = reduced.matches ? 0 : Math.floor(now / 260);
      for (let i = 0; i < 4; i++) {
        const w = 4 + ((i * 7 + tickN * 5) % 15);
        rect(x + 4, y + 4 + i * 3, w, 1, glow);
      }
    }
    rect(x + 11, y + 18, 4, 3, C.metal);
    rect(x + 7, y + 21, 12, 2, C.metal);
  }

  function keyboard(x, y) {
    rect(x, y, 20, 5, "#4a4a6a");
    for (let i = 0; i < 4; i++) rect(x + 2 + i * 4, y + 1, 2, 1, "#6c6c90");
  }

  function chair(x, y) {
    rect(x, y, 20, 10, C.chair);
    rect(x, y, 20, 2, "#5a5a82");
    rect(x + 3, y + 10, 2, 4, C.metalDark);
    rect(x + 15, y + 10, 2, 4, C.metalDark);
  }

  function cabinet(x, y) {
    rect(x, y, 28, 40, "#5f6b75");
    rect(x, y, 28, 2, "#7f8c8d");
    for (let i = 0; i < 3; i++) {
      outline(x + 2, y + 4 + i * 12, 24, 10, "#4b555e");
      rect(x + 11, y + 8 + i * 12, 6, 2, "#b2bec3");
    }
  }

  function toolbox(x, y) {
    rect(x + 8, y, 12, 3, C.metal);
    rect(x + 8, y, 2, 6, C.metal);
    rect(x + 18, y, 2, 6, C.metal);
    rect(x, y + 5, 28, 14, C.red);
    rect(x, y + 5, 28, 3, "#ff6b6b");
    rect(x + 12, y + 10, 4, 3, "#fdcb6e");
  }

  function table(x, y, w) {
    rect(x, y, w, 16, C.wood);
    rect(x, y, w, 2, C.woodLight);
    rect(x, y + 16, w, 6, C.woodDark);
    rect(x + 4, y + 22, 3, 6, C.woodDark);
    rect(x + w - 7, y + 22, 3, 6, C.woodDark);
  }

  function papers(n, x, y) {
    for (let i = 0; i < Math.min(n, 8); i++) {
      const px = x + (i % 2);
      const py = y - i * 2;
      rect(px, py, 14, 10, C.paper);
      outline(px, py, 14, 10, C.paperEdge);
      rect(px + 3, py + 3, 8, 1, C.paperEdge);
    }
  }

  function rack(x, y, now) {
    const busy = now < scene.rackBusyUntil;
    rect(x, y, 32, 54, "#1c2230");
    outline(x, y, 32, 54, "#3a4458");
    for (let i = 0; i < 5; i++) {
      rect(x + 3, y + 4 + i * 9, 26, 7, C.metalDark);
      for (let j = 0; j < 3; j++) {
        let color = (i + j) % 3 === 0 ? C.led : C.ledOff;
        if (busy && !reduced.matches) color = (Math.floor(now / 110) + i + j) % 2 ? C.ledBusy : C.led;
        rect(x + 20 + j * 3, y + 6 + i * 9, 2, 2, color);
      }
    }
    text("git", x + 16, y + 56, "#9a9ab8", 6, "center");
  }

  function printer(x, y, now) {
    rect(x, y, 40, 20, "#b2bec3");
    rect(x, y, 40, 6, "#95a5a6");
    rect(x + 6, y + 9, 28, 2, C.metalDark);
    rect(x + 33, y + 14, 3, 2, C.led);
    if (scene.printStartedAt >= 0) {
      const t = Math.min(1, (now - scene.printStartedAt) / 1600);
      const h = reduced.matches || instant ? 10 : Math.floor(t * 10);
      rect(x + 10, y + 20, 20, h, C.paper);
      if (h > 4) rect(x + 13, y + 22, 12, 1, C.paperEdge);
    }
  }

  function plant(x, y) {
    rect(x + 2, y + 12, 12, 10, "#a0522d");
    rect(x + 2, y + 12, 12, 2, "#c0703d");
    rect(x + 6, y + 2, 4, 10, "#27ae60");
    rect(x, y + 4, 6, 4, "#2ecc71");
    rect(x + 10, y + 2, 6, 4, "#2ecc71");
    rect(x + 4, y, 4, 4, "#27ae60");
  }

  function zone(x, y, w, h, color, label) {
    ctx.globalAlpha = 0.85;
    outline(x, y, w, h, color);
    outline(x + 1, y + 1, w - 2, h - 2, color);
    ctx.globalAlpha = 0.08;
    rect(x + 2, y + 2, w - 4, h - 4, color);
    ctx.globalAlpha = 1;
    text(label, x + 6, y + h - 11, color, 7, "left", "bold");
  }

  function drawRoom(now) {
    for (let ty = 42; ty < H; ty += 16) {
      for (let tx = 0; tx < W; tx += 16) {
        rect(tx, ty, 16, 16, ((tx + ty - 42) / 16) % 2 ? C.floorA : C.floorB);
      }
    }
    rect(0, 0, W, 40, C.wall);
    rect(0, 40, W, 3, C.wallTrim);
    rect(0, 43, W, 2, "rgba(0,0,0,0.3)");
    windowPane(24, 8, 64, 24);
    windowPane(392, 8, 64, 24);
    whiteboard(176, 5, 128, 30);

    zone(8, 50, 200, 134, C.auditor, "KAI (Auditor)");
    zone(272, 50, 200, 134, C.fixer, "JAX (Fixer)");

    cabinet(166, 58);
    desk(48, 76, 84);
    monitor(77, 56, scene.monitors.auditor, C.auditor, C.auditorDark, now);
    keyboard(80, 85);
    chair(80, 108);

    toolbox(284, 78);
    desk(348, 76, 84);
    monitor(377, 56, scene.monitors.fixer, C.fixer, C.fixerDark, now);
    keyboard(380, 85);
    chair(380, 108);

    table(210, 120, 60);
    papers(scene.table, 233, 126);

    rack(224, 204, now);
    printer(30, 226, now);
    plant(452, 252);
    plant(184, 252);
  }

  function drawAgent(a, now) {
    const moving = a.path.length > 0;
    const legs = moving ? (Math.floor(now / 170) % 2 ? LEGS.stepA : LEGS.stepB) : LEGS.stand;
    const bob = a.working && !moving && !reduced.matches && Math.floor(now / 340) % 2 ? 1 : 0;
    const left = Math.round(a.x) - (SPRITE_W * PX) / 2;
    const top = Math.round(a.y) - SPRITE_H * PX;
    const colors = COLORS[a.who];

    ctx.globalAlpha = 0.35;
    rect(left + 3, Math.round(a.y) - 2, 14, 3, "#000");
    ctx.globalAlpha = 1;

    grid(legs, colors, left, top + 11 * PX, PX);
    grid(TORSO, colors, left, top + 6 * PX + bob, PX);
    grid(HEADS[a.who][a.face], colors, left, top + bob, PX);

    if (a.carrying > 0 && a.face === "down") {
      rect(left + 5, top + 15, 10, 7, C.paper);
      outline(left + 5, top + 15, 10, 7, C.paperEdge);
    }
  }

  function drawBubble(a, now) {
    if (!a.bubble) return;
    const age = now - a.bubbleAt;
    if (age > BUBBLE_MS) return;
    const label = a.emote ? `${a.emote} ${a.bubble}` : a.bubble;
    const clipped = label.length > 30 ? label.slice(0, 29) + "…" : label;
    ctx.font = `7px ui-monospace, "SF Mono", Menlo, Consolas, monospace`;
    const w = Math.ceil(ctx.measureText(clipped).width) + 8;
    const h = 12;
    const ax = Math.round(a.x);
    const bx = Math.max(2, Math.min(W - w - 2, ax - Math.round(w / 2)));
    const by = Math.round(a.y) - SPRITE_H * PX - h - 6;
    ctx.globalAlpha = age > BUBBLE_MS - 2000 ? Math.max(0, (BUBBLE_MS - age) / 2000) : 1;
    rect(bx, by, w, h, "#f5f5fa");
    outline(bx, by, w, h, a.who === "auditor" ? C.auditor : C.fixer);
    rect(ax - 2, by + h, 5, 2, "#f5f5fa");
    rect(ax - 1, by + h + 2, 3, 1, "#f5f5fa");
    text(clipped, bx + 4, by + 3, C.ink, 7);
    ctx.globalAlpha = 1;
  }

  // ---------- loop and sizing ----------

  function frame(now) {
    const dt = Math.min(0.05, (now - last) / 1000 || 0);
    last = now;
    tick(agents.auditor, dt, now);
    tick(agents.fixer, dt, now);

    ctx.setTransform(renderScale, 0, 0, renderScale, 0, 0);
    ctx.imageSmoothingEnabled = false;
    drawRoom(now);
    const order = [agents.auditor, agents.fixer].sort((p, q) => p.y - q.y);
    for (const a of order) drawAgent(a, now);
    for (const a of order) drawBubble(a, now);

    viewCtx.imageSmoothingEnabled = true;
    viewCtx.imageSmoothingQuality = "high";
    viewCtx.drawImage(buffer, 0, 0, view.width, view.height);
    requestAnimationFrame(frame);
  }

  // The canvas fills its frame at the scene's aspect ratio. The scene is
  // drawn at the next whole-number scale up and shrunk to fit, which keeps
  // the pixel art sharp at any size instead of letterboxing it to the
  // largest whole multiple.
  function resize() {
    const dpr = window.devicePixelRatio || 1;
    const box = view.parentElement.getBoundingClientRect();
    const s = Math.max(0.25, Math.min((box.width * dpr) / W, (box.height * dpr) / H));
    view.width = Math.round(W * s);
    view.height = Math.round(H * s);
    view.style.width = `${view.width / dpr}px`;
    view.style.height = `${view.height / dpr}px`;
    renderScale = Math.max(1, Math.ceil(s - 0.01));
    buffer.width = W * renderScale;
    buffer.height = H * renderScale;
  }

  // ---------- choreography for each event ----------

  const A = agents.auditor;
  const F = agents.fixer;
  const base = (path) => String(path || "").split("/").pop();

  function stopWork() {
    A.working = F.working = false;
    scene.monitors.auditor = scene.monitors.fixer = false;
  }

  // The drop and the pick-up can run in either order when the walking is
  // skipped, so whichever happens second settles the table.
  function handoff(n) {
    scene.board.blocking = n;
    scene.handedOver = false;
    A.working = false;
    scene.monitors.auditor = false;
    then(A, () => { A.carrying = n; });
    go(A, "tableLeft");
    then(A, () => {
      if (!scene.handedOver) scene.table = n;
      A.carrying = 0;
      say(A, `${n} for you`, "📋");
    });
    pause(A, 500);
    go(A, "auditorIdle");

    go(F, "tableRight");
    F.queue.push({ until: () => scene.table > 0 });
    then(F, () => {
      F.carrying = n;
      scene.table = 0;
      scene.handedOver = true;
      say(F, "on it", "👍");
    });
    go(F, "fixerSeat");
    then(F, () => { F.carrying = 0; });
  }

  function commit(changed) {
    F.working = false;
    scene.monitors.fixer = false;
    go(F, "rack");
    then(F, () => {
      scene.rackBusyUntil = performance.now() + 2500;
      say(F, changed ? "pass saved" : "nothing changed", changed ? "📦" : "🤷");
    });
    pause(F, 900);
    go(F, "fixerIdle");
  }

  function printReport() {
    go(A, "printer");
    then(A, () => { scene.printStartedAt = performance.now(); say(A, "printing report", "🖨️"); });
    pause(A, 1700);
    then(A, () => { A.carrying = 1; scene.printStartedAt = -1; say(A, "report ready", "📄"); });
    go(A, "auditorIdle");
    then(A, () => { A.carrying = 0; });
  }

  function end(reason, remaining) {
    stopWork();
    if (reason === "clean") scene.board.blocking = 0;
    else if (remaining != null) scene.board.blocking = remaining;
    if (reason === "clean") {
      say(A, "all clear", "🎉");
      say(F, "all clear", "🎉");
    } else if (reason === "budget_exhausted") {
      say(A, "out of passes", "😓");
      say(F, "some left over", "😓");
    } else if (reason === "no_progress" || reason === "needs_manual_fix") {
      say(A, "rest needs a human", "🙋");
      say(F, "out of ideas", "🤷");
    } else if (reason === "fixer_ollama_timeouts") {
      say(F, "model stuck", "💤");
    } else {
      say(A, "review incomplete", "💤");
    }
  }

  function event(name, p) {
    p = p || {};
    switch (name) {
      case "worktree_created":
        scene.rackBusyUntil = performance.now() + 1200;
        say(F, "private copy ready", "🌱");
        break;
      case "iteration_start":
        scene.board.pass = p.iteration;
        A.working = true;
        scene.monitors.auditor = true;
        go(A, "auditorSeat");
        say(A, `pass ${p.iteration}`, "🔍");
        break;
      case "stack_detected":
        say(A, p.stack && p.stack.length ? p.stack.join(", ") : "unknown stack", "🧭");
        break;
      case "tool_start":
        say(A, p.tool, "🔍");
        break;
      case "tool_done":
        if (p.tool === "pytest" && p.passed == null) say(A, "tests skipped", "🤷");
        else if (p.tool === "pytest") say(A, p.passed ? "tests pass" : "tests fail", p.passed ? "🧪" : "❗");
        else say(A, `${p.tool}: ${p.count || 0}`, p.count ? "❗" : "👍");
        break;
      case "file_review_start":
        say(A, `reading ${p.candidates} files`, "📖");
        break;
      case "file_review_file":
        say(A, base(p.file), "📖");
        break;
      case "file_review_timeout":
        say(A, "model timed out", "⏳");
        break;
      case "file_review_failed":
        say(A, "model error", "⚠️");
        break;
      case "file_review_unparseable":
        say(A, "unreadable reply", "🤔");
        break;
      case "audit_aborted":
        say(A, "model stopped", "💤");
        break;
      case "improvements_start":
        say(A, "big-picture ideas", "💡");
        break;
      case "loop_continue":
        handoff(p.blocking_count);
        break;
      case "fix_start":
        F.working = true;
        scene.monitors.fixer = true;
        say(F, `${p.finding_id} ${base(p.file)}`, "🔧");
        break;
      case "fix_retry":
        say(F, `retry ${p.finding_id}`, "🔁");
        break;
      case "fix_verify_start":
        say(F, `verify ${p.finding_id}`, "🧐");
        break;
      case "fix_applied":
        scene.board.fixed += 1;
        say(F, `${p.finding_id} fixed`, "✅");
        break;
      case "fix_dismissed":
        say(F, `${p.finding_id}: false alarm`, "🙅");
        break;
      case "fix_rejected":
        say(F, `${p.finding_id} rejected`, "❌");
        break;
      case "fix_pass_aborted":
        say(F, "model stopped", "💤");
        break;
      case "fix_pass_done":
        commit(p.changed);
        break;
      case "loop_end":
        end(p.reason, p.remaining);
        break;
      case "audit_only_done":
        scene.board.blocking = p.blocking;
        stopWork();
        say(A, `${p.finding_count} findings`, p.blocking ? "❗" : "🎉");
        break;
      case "review_start":
        F.working = true;
        scene.monitors.fixer = true;
        go(F, "fixerSeat");
        say(F, "final review", "📝");
        break;
      case "review_done":
        if (p.verdict === "perfect") say(F, "looks great!", "🌟");
        else if (p.verdict === "needs_work") say(F, "needs work", "🛠️");
        else if (p.verdict === "incomplete") say(F, "review incomplete", "💤");
        else say(F, "in good shape", "👍");
        break;
      case "improve_start":
        F.working = true;
        scene.monitors.fixer = true;
        say(F, `${p.count} improvements`, "✨");
        break;
      case "improve_done":
        F.working = false;
        scene.monitors.fixer = false;
        say(F, `${p.applied} improved`, p.applied ? "✨" : "🤷");
        break;
      case "pdf_report":
        printReport();
        break;
      case "fixes_applied":
        say(F, `${p.files ? p.files.length : 0} files in your working tree`, "✨");
        break;
      case "pdf_report_failed":
        say(A, "printer jammed", "⚠️");
        break;
      case "job_error":
        stopWork();
        say(A, "run failed", "❗");
        say(F, "run failed", "❗");
        break;
      default:
        break;
    }
  }

  function reset({ mode, maxIterations }) {
    scene.board = { mode, pass: 0, max: maxIterations, blocking: null, fixed: 0 };
    scene.table = 0;
    scene.printStartedAt = -1;
    for (const a of [A, F]) {
      a.queue.length = 0;
      a.path.length = 0;
      a.carrying = 0;
      a.bubble = null;
    }
    A.working = true;
    scene.monitors.auditor = true;
    scene.monitors.fixer = false;
    F.working = false;
    go(A, "auditorSeat");
    say(A, mode === "audit" ? "auditing" : "starting", "🔍");
    go(F, "fixerIdle");
    say(F, mode === "audit" ? "audit only today" : "waiting for work", "☕");
  }

  // A head-and-shoulders portrait for the agent cards in the side panel.
  function portrait(target, who) {
    const px = 4;
    target.width = SPRITE_W * px;
    target.height = 11 * px;
    const prev = ctx;
    ctx = target.getContext("2d");
    grid(HEADS[who].down, COLORS[who], 0, 0, px);
    grid(TORSO, COLORS[who], 0, 6 * px, px);
    ctx = prev;
  }

  function mount(target) {
    view = target;
    viewCtx = view.getContext("2d");
    buffer = document.createElement("canvas");
    ctx = buffer.getContext("2d");
    resize();
    new ResizeObserver(resize).observe(view.parentElement);
    requestAnimationFrame((t) => { last = t; frame(t); });
  }

  window.ZweiOffice = {
    mount,
    reset,
    event,
    portrait,
    setInstant(on) { instant = Boolean(on); },
  };
})();
