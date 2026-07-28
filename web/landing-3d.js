// ===== VELOR AI — 3D-герой лендинга «Призмы» =====
// Парящие светящиеся многогранники (октаэдры и тетраэдры — эхо
// треугольника-логотипа) медленно вращаются в объёме, дрейфуют и
// наклоняются за курсором. Это НЕ ядро — у ядра свой дом в кабинете.
// Лёгкий рендер: рёбра (LineSegments) + вершины-точки, аддитивное свечение.

(function () {
  if (!window.THREE) return;
  const T = THREE;
  const canvas = document.getElementById('heroCanvas');
  if (!canvas) return;

  const mob = innerWidth < 760;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  // спокойная премиум-палитра: один фиолетовый акцент + оттенки, без неон-радуги
  const PAL = ['#8052ff', '#a99cff', '#6f74c4', '#5b6cff', '#cbc2ff'].map(h => new T.Color(h));

  // мягкая круглая текстура для вершин
  const dot = (() => {
    const c = document.createElement('canvas'); c.width = c.height = 64;
    const x = c.getContext('2d');
    const g = x.createRadialGradient(32, 32, 0, 32, 32, 32);
    g.addColorStop(0, 'rgba(255,255,255,1)');
    g.addColorStop(0.4, 'rgba(255,255,255,.5)');
    g.addColorStop(1, 'rgba(255,255,255,0)');
    x.fillStyle = g; x.fillRect(0, 0, 64, 64);
    return new T.CanvasTexture(c);
  })();

  const renderer = new T.WebGLRenderer({ canvas, alpha: true, antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  const scene = new T.Scene();
  const camera = new T.PerspectiveCamera(50, 1, 0.1, 100);
  camera.position.set(0, 0, 26);

  const group = new T.Group();
  scene.add(group);

  // мягкое заднее свечение
  const glowTex = dot;
  const glow = new T.Sprite(new T.SpriteMaterial({
    map: glowTex, color: 0x8052ff, transparent: true, opacity: 0.16,
    blending: T.AdditiveBlending, depthWrite: false,
  }));
  glow.scale.set(34, 34, 1);
  scene.add(glow);

  // ---- набор многогранников ----
  const SHAPES = mob ? 6 : 9;
  const shapes = [];
  const geoms = [
    () => new T.OctahedronGeometry(1, 0),
    () => new T.TetrahedronGeometry(1.15, 0),
    () => new T.IcosahedronGeometry(1, 0),
  ];

  for (let i = 0; i < SHAPES; i++) {
    const base = geoms[i % geoms.length]();
    const col = PAL[i % PAL.length];

    // рёбра
    const edges = new T.LineSegments(
      new T.EdgesGeometry(base),
      new T.LineBasicMaterial({ color: col, transparent: true, opacity: 0.5,
        blending: T.AdditiveBlending, depthWrite: false })
    );
    // вершины-искры
    const verts = new T.Points(
      base,
      new T.PointsMaterial({ size: 0.42, map: dot, color: col, transparent: true,
        blending: T.AdditiveBlending, depthWrite: false })
    );

    const holder = new T.Group();
    holder.add(edges); holder.add(verts);

    const s = 0.9 + Math.random() * 2.6;
    holder.scale.setScalar(s);
    // распределение по объёму, дальше от центра — мельче внимания
    const R = 13;
    holder.position.set(
      (Math.random() * 2 - 1) * R,
      (Math.random() * 2 - 1) * R * 0.7,
      (Math.random() * 2 - 1) * R * 0.6 - 2
    );
    holder.rotation.set(Math.random() * 6.28, Math.random() * 6.28, Math.random() * 6.28);

    shapes.push({
      obj: holder,
      spin: new T.Vector3((Math.random() - 0.5) * 0.4, (Math.random() - 0.5) * 0.5, (Math.random() - 0.5) * 0.3),
      bob: 0.4 + Math.random() * 0.8,
      bph: Math.random() * 6.28,
      by: holder.position.y,
    });
    group.add(holder);
  }

  // ---- размер по родителю (герой) ----
  function resize() {
    const r = canvas.parentElement.getBoundingClientRect();
    const w = r.width || innerWidth, h = r.height || innerHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h; camera.updateProjectionMatrix();
  }
  resize();
  if (window.ResizeObserver) new ResizeObserver(resize).observe(canvas.parentElement);
  else addEventListener('resize', resize);

  // виден ли герой на экране (не жжём GPU при прокрутке вниз)
  let onScreen = true;
  if (window.IntersectionObserver) {
    new IntersectionObserver(es => { onScreen = es[0].isIntersecting; }, { threshold: 0.01 }).observe(canvas);
  }

  // наклон за курсором
  let mx = 0, my = 0, smx = 0, smy = 0;
  addEventListener('mousemove', e => {
    mx = (e.clientX / innerWidth) * 2 - 1;
    my = -(e.clientY / innerHeight) * 2 + 1;
  });
  // клик — импульс: все фигуры разом крутанёт
  let kick = 0;
  addEventListener('click', () => { kick = 1; });

  const clock = new T.Clock();
  let t = 0;
  (function loop() {
    requestAnimationFrame(loop);
    if (!onScreen) { clock.getDelta(); return; }
    let dt = Math.min(clock.getDelta(), 0.05);
    if (reduced) dt *= 0.15;
    t += dt;
    if (kick > 0) kick = Math.max(0, kick - dt * 1.2);

    smx += (mx - smx) * 0.05; smy += (my - smy) * 0.05;

    for (const sh of shapes) {
      const k = 1 + kick * 3;
      sh.obj.rotation.x += sh.spin.x * dt * k;
      sh.obj.rotation.y += sh.spin.y * dt * k;
      sh.obj.rotation.z += sh.spin.z * dt * k;
      sh.obj.position.y = sh.by + Math.sin(t * sh.bob + sh.bph) * 0.6;
    }

    // весь ансамбль медленно вращается; параллакс даёт камера
    group.rotation.y += dt * (0.05 + kick * 0.2);
    group.rotation.x += ((smy * 0.2) - group.rotation.x) * 0.05;
    camera.position.x += (smx * 3 - camera.position.x) * 0.04;
    camera.position.y += (smy * 2 - camera.position.y) * 0.04;
    camera.lookAt(0, 0, 0);

    glow.material.opacity = 0.22 + kick * 0.2;

    renderer.render(scene, camera);
  })();
})();
