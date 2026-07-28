// ===== VELOR — ЖИВОЕ ЯДРО v11 «Разум» (Dala / OLED) =====
// Не декоративная анимация, а ощущение работающего интеллекта: ядро медленно
// «дышит», центральное свечение живое, внутренние нити постепенно перестраиваются,
// внешнее кольцо реагирует очень мягко. Движение — сумма несоизмеримых частот,
// поэтому оно никогда не повторяется буквально. Работает само по себе, без действий
// пользователя. Состояния переключаются плавно: покой, думает, генерирует ответ,
// анализирует документы, нашёл рекомендацию (золотой импульс), ошибка (мягкий сдвиг
// оттенка, без резкой вспышки). Управление: window.VELOR_CORE.setState('...').

(function () {
  if (!window.THREE) return;
  const T = THREE;
  const mob = innerWidth < 760;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;

  const PAL = ['#8052ff', '#a99cff', '#7a7fd0', '#5b6cff', '#cbc2ff'].map(h => new T.Color(h));
  const WHITE = new T.Color('#ffffff');
  const tmp = new T.Color();

  const mkTex = stops => {
    const c = document.createElement('canvas'); c.width = c.height = 128;
    const x = c.getContext('2d');
    const g = x.createRadialGradient(64, 64, 0, 64, 64, 64);
    for (const [o, col] of stops) g.addColorStop(o, col);
    x.fillStyle = g; x.fillRect(0, 0, 128, 128);
    return new T.CanvasTexture(c);
  };
  const sharpTex = mkTex([[0, 'rgba(255,255,255,1)'], [0.3, 'rgba(255,255,255,0.9)'], [0.55, 'rgba(255,255,255,0.25)'], [1, 'rgba(255,255,255,0)']]);
  const softTex = mkTex([[0, 'rgba(255,255,255,1)'], [0.4, 'rgba(255,255,255,0.45)'], [1, 'rgba(255,255,255,0)']]);

  const canvas = document.getElementById('coreCanvas');
  if (canvas) initCore();

  function initCore() {
    const renderer = new T.WebGLRenderer({ canvas, alpha: true, antialias: true });
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    const scene = new T.Scene();
    const camera = new T.PerspectiveCamera(45, 1, 0.1, 100);
    camera.position.set(0, 0, 30);

    const R = 9;
    const group = new T.Group();
    scene.add(group);

    // ================= УЗЛЫ (внутренняя структура) =================
    const N = mob ? 150 : 240;
    const nodePos = [], basePos = [];
    const nodeCharge = new Float32Array(N);
    const nodeHue = new Uint8Array(N);
    // три очень медленные несоизмеримые частоты дрейфа на узел — движение живое,
    // но неспешное и никогда не повторяющееся буквально
    const dFq = new Float32Array(N * 3), dPh = new Float32Array(N * 3), dAmp = new Float32Array(N);
    for (let i = 0; i < N; i++) {
      const v = new T.Vector3().randomDirection();
      const r = R * (0.28 + 0.72 * Math.pow(Math.random(), 0.4));
      basePos.push(v.clone().multiplyScalar(r));
      nodePos.push(v.multiplyScalar(r));
      nodeHue[i] = (Math.random() * PAL.length) | 0;
      for (let k = 0; k < 3; k++) {
        dFq[i * 3 + k] = 0.10 + Math.random() * 0.35;
        dPh[i * 3 + k] = Math.random() * 6.28;
      }
      dAmp[i] = 0.3 + Math.random() * 0.5;
    }

    // ================= НИТИ (медленно перестраиваются) =================
    const K = 6;
    const edgeSet = new Set(); const edges = [];
    for (let i = 0; i < N; i++) {
      const d = [];
      for (let j = 0; j < N; j++) if (j !== i) d.push([nodePos[i].distanceToSquared(nodePos[j]), j]);
      d.sort((x, y) => x[0] - y[0]);
      for (let k = 0; k < K; k++) {
        const j = d[k][1];
        const key = i < j ? i * N + j : j * N + i;
        if (edgeSet.has(key)) continue;
        edgeSet.add(key);
        // vf/vp — очень медленный цикл видимости нити: одни гаснут, другие
        // проявляются, из-за чего структура постоянно, но плавно перестраивается
        edges.push({ a: i, b: j, len: Math.sqrt(d[k][0]), glow: 0,
          vf: 0.03 + Math.random() * 0.06, vp: Math.random() * 6.28 });
      }
    }
    const E = edges.length;
    const nbr = Array.from({ length: N }, () => []);
    edges.forEach((e, ei) => { nbr[e.a].push(ei); nbr[e.b].push(ei); });

    const lPos = new Float32Array(E * 6), lCol = new Float32Array(E * 6);
    edges.forEach((e, i) => { const A = nodePos[e.a], B = nodePos[e.b]; lPos.set([A.x, A.y, A.z, B.x, B.y, B.z], i * 6); });
    const lGeo = new T.BufferGeometry();
    lGeo.setAttribute('position', new T.BufferAttribute(lPos, 3));
    lGeo.setAttribute('color', new T.BufferAttribute(lCol, 3));
    const lines = new T.LineSegments(lGeo, new T.LineBasicMaterial({
      vertexColors: true, transparent: true, opacity: 0.9,
      blending: T.AdditiveBlending, depthWrite: false,
    }));
    group.add(lines);

    // узлы: чёткое ядрышко + мягкий ореол
    const nPos = new Float32Array(N * 3), nCol = new Float32Array(N * 3);
    nodePos.forEach((p, i) => nPos.set([p.x, p.y, p.z], i * 3));
    const nGeo = new T.BufferGeometry();
    nGeo.setAttribute('position', new T.BufferAttribute(nPos, 3));
    nGeo.setAttribute('color', new T.BufferAttribute(nCol, 3));
    const nodesSharp = new T.Points(nGeo, new T.PointsMaterial({
      size: 0.42, map: sharpTex, vertexColors: true, transparent: true,
      blending: T.AdditiveBlending, depthWrite: false,
    }));
    const nodesHalo = new T.Points(nGeo, new T.PointsMaterial({
      size: 1.5, map: softTex, vertexColors: true, transparent: true,
      opacity: 0.5, blending: T.AdditiveBlending, depthWrite: false,
    }));
    group.add(nodesSharp); group.add(nodesHalo);

    // ================= СИГНАЛЫ (потоки мысли) =================
    const SMAX = mob ? 120 : 240;
    const sigs = [];
    const sPos = new Float32Array(SMAX * 3), sCol = new Float32Array(SMAX * 3);
    const sGeo = new T.BufferGeometry();
    sGeo.setAttribute('position', new T.BufferAttribute(sPos, 3));
    sGeo.setAttribute('color', new T.BufferAttribute(sCol, 3));
    const sigPoints = new T.Points(sGeo, new T.PointsMaterial({
      size: 0.95, map: sharpTex, vertexColors: true, transparent: true,
      blending: T.AdditiveBlending, depthWrite: false,
    }));
    group.add(sigPoints);
    function fire(node, ci, hops) {
      if (sigs.length >= SMAX) return;
      const es = nbr[node]; if (!es.length) return;
      const ei = es[(Math.random() * es.length) | 0];
      sigs.push({ ei, from: node, t: 0, spd: 4 + Math.random() * 3, ci, hops });
    }

    // ================= ВНЕШНЕЕ КОЛЬЦО (реагирует очень мягко) =================
    const RN = mob ? 70 : 120;
    const ringAng = new Float32Array(RN);
    for (let i = 0; i < RN; i++) ringAng[i] = i / RN * 6.283;
    const rPos = new Float32Array(RN * 3), rCol = new Float32Array(RN * 3);
    const rGeo = new T.BufferGeometry();
    rGeo.setAttribute('position', new T.BufferAttribute(rPos, 3));
    rGeo.setAttribute('color', new T.BufferAttribute(rCol, 3));
    const ring = new T.Points(rGeo, new T.PointsMaterial({
      size: 0.5, map: softTex, vertexColors: true, transparent: true,
      opacity: 0.9, blending: T.AdditiveBlending, depthWrite: false,
    }));
    const ringGroup = new T.Group(); ringGroup.add(ring); scene.add(ringGroup);

    // ================= ЦЕНТРАЛЬНОЕ СВЕЧЕНИЕ (живое) =================
    const glow = new T.Sprite(new T.SpriteMaterial({
      map: softTex, color: 0x8052ff, transparent: true, opacity: 0.4,
      blending: T.AdditiveBlending, depthWrite: false,
    }));
    glow.scale.set(18, 18, 1); scene.add(glow);
    const core = new T.Sprite(new T.SpriteMaterial({
      map: softTex, color: 0xffffff, transparent: true, opacity: 0.5,
      blending: T.AdditiveBlending, depthWrite: false,
    }));
    core.scale.set(6, 6, 1); scene.add(core);

    // ================= СОСТОЯНИЯ =================
    const IRIS = new T.Color('#8052ff'), LIL = new T.Color('#a99cff'),
          GOLD = new T.Color('#ffb829'), CORAL = new T.Color('#ff6b6b');
    // drift — амплитуда дрейфа узлов; sig — частота потоков мысли; rot — скорость
    // вращения; pulse — мягкая пульсация («генерация»); breath — глубина дыхания;
    // density — сколько нитей проявлено («анализ» проявляет больше); tint/tintAmt — оттенок.
    const STATES = {
      idle:       { drift: 0.55, sig: 0.6, rot: 0.045, pulse: 0, breath: 1.00, density: 0.58, tint: IRIS,  tintAmt: 0.00 },
      thinking:   { drift: 1.05, sig: 4.2, rot: 0.090, pulse: 0, breath: 1.10, density: 0.80, tint: LIL,   tintAmt: 0.18 },
      generating: { drift: 0.75, sig: 2.0, rot: 0.060, pulse: 1, breath: 1.35, density: 0.72, tint: IRIS,  tintAmt: 0.00 },
      analyzing:  { drift: 0.70, sig: 2.6, rot: 0.060, pulse: 0, breath: 1.05, density: 1.00, tint: IRIS,  tintAmt: 0.05 },
      error:      { drift: 0.40, sig: 0.5, rot: 0.040, pulse: 0, breath: 0.95, density: 0.55, tint: CORAL, tintAmt: 0.30 },
    };
    const cur = Object.assign({}, STATES.idle);
    const curTint = IRIS.clone();
    let target = STATES.idle, targetName = 'idle';
    let insight = 0, errTimer = 0;

    function setState(name) {
      if (name === 'insight' || name === 'pulse') { insight = 1; return; }  // короткий золотой импульс
      if (!STATES[name]) return;
      target = STATES[name]; targetName = name;
      if (name === 'error') errTimer = 3.0;   // ошибка сама мягко возвращается в покой
    }
    // Публичный API ядра — им пользуются страницы (напр. чат в home.html):
    //   VELOR_CORE.setState('thinking' | 'generating' | 'analyzing' | 'error' | 'idle')
    //   VELOR_CORE.insight()  — нашлась важная рекомендация (золотой импульс)
    window.VELOR_CORE = { setState, state: setState, insight: () => { insight = 1; }, pulse: () => { insight = 1; } };
    window.ARINA_CORE_PULSE = () => { insight = 1; };            // совместимость
    canvas.addEventListener('click', () => { insight = 1; });

    // ---------- размер / видимость / качество ----------
    function resize() {
      const r = canvas.parentElement.getBoundingClientRect();
      const s = Math.min(r.width, r.height) || 400;
      renderer.setSize(s, s, false);
      camera.aspect = 1; camera.updateProjectionMatrix();
    }
    resize();
    if (window.ResizeObserver) new ResizeObserver(resize).observe(canvas.parentElement);
    else addEventListener('resize', resize);

    let onScreen = true;
    if (window.IntersectionObserver) {
      new IntersectionObserver(es => { onScreen = es[0].isIntersecting; }, { threshold: 0.01 }).observe(canvas);
    }
    let frames = 0, fpsT = 0, quality = Math.min(devicePixelRatio, 2);

    // мягкий наклон сцены за курсором (по всей странице)
    let mx = 0, my = 0, smx = 0, smy = 0;
    addEventListener('mousemove', e => {
      mx = (e.clientX / innerWidth) * 2 - 1;
      my = -(e.clientY / innerHeight) * 2 + 1;
    });

    // ---------- анимация ----------
    const clock = new T.Clock();
    let t = 0, sigAcc = 0;
    (function loop() {
      requestAnimationFrame(loop);
      if (!onScreen) { clock.getDelta(); return; }
      let dt = Math.min(clock.getDelta(), 0.05);
      if (reduced) dt *= 0.25;
      t += dt;

      frames++; fpsT += dt / (reduced ? 0.25 : 1);
      if (fpsT >= 2) {
        const fps = frames / fpsT;
        if (fps < 42 && quality > 1) { quality = 1; renderer.setPixelRatio(1); }
        frames = 0; fpsT = 0;
      }

      // плавный переход параметров состояния (никаких резких скачков)
      const e = Math.min(1, dt * 0.7);
      cur.drift += (target.drift - cur.drift) * e;
      cur.sig += (target.sig - cur.sig) * e;
      cur.rot += (target.rot - cur.rot) * e;
      cur.pulse += (target.pulse - cur.pulse) * e;
      cur.breath += (target.breath - cur.breath) * e;
      cur.density += (target.density - cur.density) * e;
      cur.tintAmt += (target.tintAmt - cur.tintAmt) * e;
      curTint.lerp(target.tint, e);
      if (errTimer > 0) { errTimer -= dt; if (errTimer <= 0 && targetName === 'error') { target = STATES.idle; targetName = 'idle'; } }
      if (insight > 0) insight = Math.max(0, insight - dt * 0.8);
      smx += (mx - smx) * 0.05; smy += (my - smy) * 0.05;

      // дыхание — сумма несоизмеримых синусов, поэтому ритм живой и не повторяется
      const breath = 0.6 * Math.sin(t * 0.21) + 0.28 * Math.sin(t * 0.37 + 1.3) + 0.12 * Math.sin(t * 0.59 + 2.1);
      const gen = cur.pulse * 0.12 * Math.sin(t * 1.4);           // мягкая пульсация «генерации»
      group.scale.setScalar(1 + (0.028 * breath + gen) * cur.breath);

      // дрейф узлов (внутренняя структура медленно живёт)
      const amp = cur.drift;
      for (let i = 0; i < N; i++) {
        const a = dAmp[i] * amp + nodeCharge[i] * 0.4;
        const p = nodePos[i], bp = basePos[i];
        p.x = bp.x + Math.sin(t * dFq[i * 3] + dPh[i * 3]) * a;
        p.y = bp.y + Math.sin(t * dFq[i * 3 + 1] + dPh[i * 3 + 1]) * a;
        p.z = bp.z + Math.sin(t * dFq[i * 3 + 2] + dPh[i * 3 + 2]) * a;
        nPos[i * 3] = p.x; nPos[i * 3 + 1] = p.y; nPos[i * 3 + 2] = p.z;
      }
      nGeo.attributes.position.needsUpdate = true;

      // потоки мысли рождаются с частотой текущего состояния (идут и в покое)
      if (!reduced) {
        sigAcc += dt * cur.sig;
        while (sigAcc >= 1) { sigAcc -= 1; const i = (Math.random() * N) | 0; nodeCharge[i] = Math.max(nodeCharge[i], 0.85); fire(i, nodeHue[i], 2); }
      }
      // золотой импульс из центра при «нашёл рекомендацию»
      if (insight > 0.6 && Math.random() < 0.35) {
        for (let i = 0; i < N; i++) if (nodePos[i].length() < R * 0.5) { nodeCharge[i] = 1; if (Math.random() < 0.5) fire(i, 0, 3); }
      }
      // движение сигналов по нитям
      for (let s = sigs.length - 1; s >= 0; s--) {
        const g = sigs[s], ed = edges[g.ei];
        g.t += dt * g.spd / ed.len; ed.glow = Math.max(ed.glow, 1);
        if (g.t >= 1) { const arr = (g.from === ed.a) ? ed.b : ed.a; nodeCharge[arr] = 1; sigs.splice(s, 1); if (g.hops > 0 && Math.random() < 0.7) fire(arr, g.ci, g.hops - 1); }
      }

      // нити: позиции тянутся за узлами; видимость медленно дышит (перестройка)
      for (let i = 0; i < E; i++) {
        const ed = edges[i], A = nodePos[ed.a], B = nodePos[ed.b];
        lPos[i * 6] = A.x; lPos[i * 6 + 1] = A.y; lPos[i * 6 + 2] = A.z;
        lPos[i * 6 + 3] = B.x; lPos[i * 6 + 4] = B.y; lPos[i * 6 + 5] = B.z;
        if (ed.glow > 0) ed.glow = Math.max(0, ed.glow - dt * 2.0);
        let vis = 0.5 + 0.5 * Math.sin(t * ed.vf + ed.vp);
        vis = Math.max(0, vis - (1 - cur.density));       // density проявляет больше нитей («анализ»)
        const chA = nodeCharge[ed.a], chB = nodeCharge[ed.b];
        const base = vis * (0.10 + cur.density * 0.18) + ed.glow * 0.8;
        const bA = base + chA * 0.4, bB = base + chB * 0.4;
        tmp.copy(curTint).lerp(WHITE, ed.glow * 0.5);
        if (insight > 0) tmp.lerp(GOLD, insight * 0.5);
        lCol[i * 6] = tmp.r * bA; lCol[i * 6 + 1] = tmp.g * bA; lCol[i * 6 + 2] = tmp.b * bA;
        lCol[i * 6 + 3] = tmp.r * bB; lCol[i * 6 + 4] = tmp.g * bB; lCol[i * 6 + 5] = tmp.b * bB;
      }
      lGeo.attributes.position.needsUpdate = true; lGeo.attributes.color.needsUpdate = true;

      // позиции и цвет сигналов
      for (let s = 0; s < SMAX; s++) {
        if (s < sigs.length) {
          const g = sigs[s], ed = edges[g.ei];
          const A = nodePos[g.from === ed.a ? ed.a : ed.b], B = nodePos[g.from === ed.a ? ed.b : ed.a];
          sPos[s * 3] = A.x + (B.x - A.x) * g.t; sPos[s * 3 + 1] = A.y + (B.y - A.y) * g.t; sPos[s * 3 + 2] = A.z + (B.z - A.z) * g.t;
          tmp.copy(PAL[g.ci]).lerp(WHITE, 0.5); if (insight > 0) tmp.lerp(GOLD, insight * 0.6);
          sCol[s * 3] = tmp.r * 2; sCol[s * 3 + 1] = tmp.g * 2; sCol[s * 3 + 2] = tmp.b * 2;
        } else { sPos[s * 3] = 0; sPos[s * 3 + 1] = 0; sPos[s * 3 + 2] = 999; }
      }
      sGeo.attributes.position.needsUpdate = true; sGeo.attributes.color.needsUpdate = true;

      // узлы: цвет = свой тон (+ оттенок состояния) + вспышка заряда + золото инсайта
      const twB = 0.5 + 0.12 * Math.sin(t * 0.9);
      for (let i = 0; i < N; i++) {
        if (nodeCharge[i] > 0) nodeCharge[i] = Math.max(0, nodeCharge[i] - dt * 1.3);
        const ch = nodeCharge[i];
        tmp.copy(PAL[nodeHue[i]]).lerp(curTint, cur.tintAmt);
        const tw = 0.7 + 0.3 * Math.sin(t * 1.3 + i * 1.7);
        let b = twB * tw + ch * 3.0 + cur.pulse * 0.25 * (0.5 + 0.5 * Math.sin(t * 1.4));
        tmp.lerp(WHITE, Math.min(1, ch * 0.9));
        if (insight > 0) { tmp.lerp(GOLD, insight * 0.5); b += insight * 0.6; }
        nCol[i * 3] = tmp.r * b; nCol[i * 3 + 1] = tmp.g * b; nCol[i * 3 + 2] = tmp.b * b;
      }
      nGeo.attributes.color.needsUpdate = true;

      // внешнее кольцо — реагирует очень мягко (лёгкое расширение и подсветка)
      const ringReact = cur.pulse * 0.05 + insight * 0.14;
      const rr = R * 1.22 * (1 + 0.02 * breath + ringReact);
      tmp.copy(curTint); if (insight > 0) tmp.lerp(GOLD, insight * 0.6);
      const rb = 0.42 + 0.16 * Math.sin(t * 0.5) + ringReact * 2;
      for (let i = 0; i < RN; i++) {
        const a = ringAng[i];
        const wob = 1 + 0.04 * Math.sin(a * 3 + t * 0.3) + 0.03 * Math.sin(a * 5 - t * 0.2);
        rPos[i * 3] = Math.cos(a) * rr * wob; rPos[i * 3 + 1] = Math.sin(a) * rr * wob; rPos[i * 3 + 2] = Math.sin(a * 2 + t * 0.15) * 0.4;
        const tw = 0.6 + 0.4 * Math.sin(a * 4 + t * 0.6 + i);
        const bb = rb * tw;
        rCol[i * 3] = tmp.r * bb; rCol[i * 3 + 1] = tmp.g * bb; rCol[i * 3 + 2] = tmp.b * bb;
      }
      rGeo.attributes.position.needsUpdate = true; rGeo.attributes.color.needsUpdate = true;
      ringGroup.rotation.z += dt * 0.03;                  // очень медленно
      ringGroup.rotation.x += ((0.35 + smy * 0.15) - ringGroup.rotation.x) * 0.04;
      ringGroup.rotation.y += ((smx * 0.15) - ringGroup.rotation.y) * 0.04;
      ring.material.opacity = 0.32 + ringReact * 1.4;

      // центральное свечение — живое: цвет по состоянию, тёплое золото при инсайте
      tmp.copy(curTint); if (insight > 0) tmp.lerp(GOLD, insight * 0.7);
      glow.material.color.copy(tmp);
      glow.scale.setScalar((17 + insight * 4) * (1 + 0.06 * breath + gen * 1.5));
      glow.material.opacity = 0.32 + cur.pulse * 0.12 * (0.5 + 0.5 * Math.sin(t * 1.4)) + insight * 0.4;
      core.material.color.copy(WHITE).lerp(tmp, 0.3 + cur.tintAmt * 0.4);
      core.scale.setScalar((5.5 + insight * 2) * (1 + 0.08 * breath + gen * 2));
      core.material.opacity = 0.4 + insight * 0.4 + cur.pulse * 0.1 * (0.5 + 0.5 * Math.sin(t * 1.4));

      // вращение — очень медленное, «живое» (оси мягко покачиваются, скорость плавает)
      group.rotation.y += dt * cur.rot;
      group.rotation.x += ((smy * 0.35 + Math.sin(t * 0.33) * 0.06) - group.rotation.x) * 0.04;
      group.rotation.z += ((-smx * 0.15 + Math.cos(t * 0.27) * 0.05) - group.rotation.z) * 0.04;
      group.updateMatrixWorld();

      renderer.render(scene, camera);
    })();
  }

  // ============================================================
  //  АМБИЕНТ-ПОЛЕ: частицы-звёзды, между близкими — тонкие нити;
  //  курсор расталкивает частицы и подсвечивает связи; клик — волна.
  // ============================================================
  const amb = document.getElementById('ambientCanvas');
  if (amb) initAmbient();

  function initAmbient() {
    const ctx = amb.getContext('2d');
    const COLORS = ['#8052ff', '#a99cff', '#7a7fd0', '#5b6cff', '#cbc2ff', '#8a8fb0'];
    let W, H, pts = [];
    const NP = mob ? 55 : 130;
    const LINK = mob ? 110 : 140;
    const CUR = 190;
    function resize() {
      W = amb.width = innerWidth;
      H = amb.height = innerHeight;
      pts = Array.from({ length: NP }, () => ({
        x: Math.random() * W, y: Math.random() * H,
        vx: (Math.random() - 0.5) * 14, vy: (Math.random() - 0.5) * 14,
        r: 1 + Math.random() * 1.8,
        c: COLORS[(Math.random() * COLORS.length) | 0],
        ph: Math.random() * 6.28, tw: 0.4 + Math.random() * 1.2,
        heat: 0,
      }));
    }
    resize();
    addEventListener('resize', resize);

    let cx = -9999, cy = -9999;
    addEventListener('mousemove', e => { cx = e.clientX; cy = e.clientY; });
    addEventListener('mouseout', e => { if (!e.relatedTarget) { cx = cy = -9999; } });
    const waves = [];
    addEventListener('click', e => {
      if (waves.length < 4) waves.push({ x: e.clientX, y: e.clientY, r: 0 });
    });

    let last = performance.now();
    (function loop(now) {
      requestAnimationFrame(loop);
      let dt = Math.min((now - last) / 1000, 0.05); last = now;
      if (reduced) dt = 0;
      ctx.clearRect(0, 0, W, H);
      const t = now / (reduced ? 4000 : 1000);

      for (let i = waves.length - 1; i >= 0; i--) {
        const w = waves[i];
        w.r += dt * 900;
        if (w.r > Math.max(W, H) * 1.2) { waves.splice(i, 1); continue; }
        const a = Math.max(0, 1 - w.r / (Math.max(W, H)));
        ctx.beginPath();
        ctx.arc(w.x, w.y, w.r, 0, 6.283);
        ctx.strokeStyle = '#8052ff';
        ctx.globalAlpha = a * 0.35;
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }

      for (const p of pts) {
        const dxc = p.x - cx, dyc = p.y - cy;
        const dc2 = dxc * dxc + dyc * dyc;
        if (dc2 < CUR * CUR && dc2 > 1) {
          const dcl = Math.sqrt(dc2);
          const f = (1 - dcl / CUR) * 60 * dt;
          p.x += (dxc / dcl) * f; p.y += (dyc / dcl) * f;
          p.heat = Math.max(p.heat, 1 - dcl / CUR);
        }
        for (const w of waves) {
          const dw = Math.abs(Math.hypot(p.x - w.x, p.y - w.y) - w.r);
          if (dw < 40) p.heat = Math.max(p.heat, 1 - dw / 40);
        }
        if (p.heat > 0) p.heat = Math.max(0, p.heat - dt * 1.4);

        p.x += p.vx * dt; p.y += p.vy * dt;
        if (p.x < -10) p.x = W + 10; else if (p.x > W + 10) p.x = -10;
        if (p.y < -10) p.y = H + 10; else if (p.y > H + 10) p.y = -10;
      }

      ctx.lineWidth = 1;
      for (let i = 0; i < NP; i++) {
        const a = pts[i];
        for (let j = i + 1; j < NP; j++) {
          const b = pts[j];
          const dx = a.x - b.x, dy = a.y - b.y;
          const d2 = dx * dx + dy * dy;
          if (d2 > LINK * LINK) continue;
          const d = Math.sqrt(d2);
          const heat = Math.max(a.heat, b.heat);
          ctx.beginPath();
          ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
          ctx.strokeStyle = heat > 0.05 ? a.c : '#8052ff';
          ctx.globalAlpha = (1 - d / LINK) * (0.07 + heat * 0.35);
          ctx.stroke();
        }
        const dxc = a.x - cx, dyc = a.y - cy;
        const dc2 = dxc * dxc + dyc * dyc;
        if (dc2 < CUR * CUR) {
          const dc = Math.sqrt(dc2);
          ctx.beginPath();
          ctx.moveTo(a.x, a.y); ctx.lineTo(cx, cy);
          ctx.strokeStyle = a.c;
          ctx.globalAlpha = (1 - dc / CUR) * 0.30;
          ctx.stroke();
        }
      }

      for (const p of pts) {
        const twk = 0.35 + 0.3 * (0.5 + 0.5 * Math.sin(p.ph + t * p.tw));
        const al = Math.min(1, twk + p.heat * 0.8);
        const rr = p.r * (1 + p.heat * 1.6);
        ctx.beginPath();
        ctx.arc(p.x, p.y, rr, 0, 6.283);
        ctx.fillStyle = p.heat > 0.4 ? '#ffffff' : p.c;
        ctx.globalAlpha = al;
        ctx.fill();
      }
      ctx.globalAlpha = 1;
    })(last);
  }
})();
