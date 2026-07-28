// ===== ARINA CORE — общий «движок» оформления =====
// 3D-фон (звёзды/туманности/волновое поле/ИИ-сфера) + курсор-перекрестие + часы + style-hover.
// Используется и лендингом, и панелью, чтобы вид был идентичным.
class ARINACore {
  constructor() {
    this.props = { showSphere: true, motion: 1, showCursor: true };
    this._cl = [];
    this._motion = this.props.motion;
    this._fine = matchMedia('(hover:hover) and (pointer:fine)').matches;
    this.initHover();
    this.initClock();
    this.initCursor();
    this.initThree();
  }

  initHover() {
    document.querySelectorAll('[style-hover]').forEach(el => {
      const base = el.getAttribute('style') || '';
      const hover = el.getAttribute('style-hover');
      el.addEventListener('mouseenter', () => el.setAttribute('style', base + ';' + hover));
      el.addEventListener('mouseleave', () => el.setAttribute('style', base));
    });
  }

  initClock() {
    const el = document.getElementById('hclock');
    if (!el) return;
    const tick = () => {
      const d = new Date(), p = n => String(n).padStart(2, '0');
      el.textContent = p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
    };
    tick();
    setInterval(tick, 250);
  }

  initCursor() {
    if (!this._fine) return;
    const H = document.getElementById('curH'), V = document.getElementById('curV');
    const R = document.getElementById('curRing'), XY = document.getElementById('curXY');
    if (!H || !V || !R || !XY) return;
    const mv = e => {
      H.style.transform = 'translateY(' + e.clientY + 'px)';
      V.style.transform = 'translateX(' + e.clientX + 'px)';
      R.style.transform = 'translate(' + e.clientX + 'px,' + e.clientY + 'px)';
      XY.style.transform = 'translate(' + (e.clientX + 22) + 'px,' + (e.clientY + 16) + 'px)';
      const p = n => String(Math.max(0, n | 0)).padStart(4, '0');
      XY.textContent = 'X:' + p(e.clientX) + ' Y:' + p(e.clientY);
    };
    addEventListener('mousemove', mv);
    // кольцо увеличивается над интерактивными элементами
    const over = e => {
      const hot = e.target.closest && e.target.closest('a,button,[data-row]');
      R.style.width = hot ? '46px' : '28px';
      R.style.height = hot ? '46px' : '28px';
      R.style.margin = hot ? '-23px 0 0 -23px' : '-14px 0 0 -14px';
      R.style.borderColor = hot ? 'rgba(255,107,214,.85)' : 'rgba(143,233,255,.8)';
      R.style.transition = 'width .25s,height .25s,margin .25s,border-color .25s';
    };
    addEventListener('mouseover', over);
  }

  // ---------- 3D фон ----------
  initThree() {
    if (!window.THREE) { setTimeout(() => this.initThree(), 200); return; }
    const T = THREE;
    const canvas = document.getElementById('bg3d');
    if (!canvas) return;
    const mob = innerWidth < 760;

    const renderer = new T.WebGLRenderer({ canvas, alpha: true, antialias: false });
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    renderer.setSize(innerWidth, innerHeight);
    const scene = new T.Scene();
    const camera = new T.PerspectiveCamera(60, innerWidth / innerHeight, 0.1, 500);
    camera.position.set(0, 0, 62);

    const dotTex = (soft) => {
      const c = document.createElement('canvas'); c.width = c.height = 64;
      const x = c.getContext('2d');
      const g = x.createRadialGradient(32, 32, 0, 32, 32, 32);
      g.addColorStop(0, 'rgba(255,255,255,1)');
      g.addColorStop(soft ? 0.25 : 0.4, 'rgba(255,255,255,' + (soft ? 0.4 : 0.9) + ')');
      g.addColorStop(1, 'rgba(255,255,255,0)');
      x.fillStyle = g; x.fillRect(0, 0, 64, 64);
      return new T.CanvasTexture(c);
    };
    const texHard = dotTex(false), texSoft = dotTex(true);
    const PAL = ['#8fe9ff', '#6a7bff', '#b46bff', '#ff6bd6', '#5affd0'].map(h => new T.Color(h));

    const starLayers = [];
    [[mob ? 500 : 1400, 220, 0.7, 0.35], [mob ? 300 : 800, 150, 1.1, 0.55], [mob ? 150 : 400, 100, 1.7, 0.8]].forEach(([n, r, sz, op], k) => {
      const pos = new Float32Array(n * 3);
      for (let i = 0; i < n; i++) {
        const v = new T.Vector3().randomDirection().multiplyScalar(r * (0.5 + Math.random() * 0.5));
        pos.set([v.x, v.y, v.z], i * 3);
      }
      const g = new T.BufferGeometry();
      g.setAttribute('position', new T.BufferAttribute(pos, 3));
      const m = new T.PointsMaterial({ size: sz, map: texHard, transparent: true, opacity: op, color: 0xdde6ff, blending: T.AdditiveBlending, depthWrite: false });
      const p = new T.Points(g, m);
      p.userData.speed = 0.004 * (k + 1);
      scene.add(p); starLayers.push(p);
    });

    const nebulae = [];
    for (let i = 0; i < (mob ? 3 : 6); i++) {
      const m = new T.SpriteMaterial({ map: texSoft, color: PAL[i % PAL.length], transparent: true, opacity: 0.1, blending: T.AdditiveBlending, depthWrite: false });
      const s = new T.Sprite(m);
      s.position.set((Math.random() - 0.5) * 200, (Math.random() - 0.4) * 100, -120 - Math.random() * 80);
      const sc = 70 + Math.random() * 80; s.scale.set(sc, sc, 1);
      s.userData = { ph: Math.random() * 6.28, dx: (Math.random() - 0.5) * 0.6, dy: (Math.random() - 0.5) * 0.35, base: 0.07 + Math.random() * 0.06 };
      scene.add(s); nebulae.push(s);
    }

    const CX = mob ? 80 : 150, CZ = mob ? 50 : 95, N = CX * CZ;
    const wPos = new Float32Array(N * 3), wCol = new Float32Array(N * 3);
    for (let i = 0, k = 0; i < CX; i++) for (let j = 0; j < CZ; j++, k++) {
      wPos[k * 3] = (i / (CX - 1) - 0.5) * 220;
      wPos[k * 3 + 1] = -26;
      wPos[k * 3 + 2] = 30 - (j / (CZ - 1)) * 110;
    }
    const wGeo = new T.BufferGeometry();
    wGeo.setAttribute('position', new T.BufferAttribute(wPos, 3));
    wGeo.setAttribute('color', new T.BufferAttribute(wCol, 3));
    const wMat = new T.PointsMaterial({ size: 0.45, map: texHard, vertexColors: true, transparent: true, opacity: 0.85, blending: T.AdditiveBlending, depthWrite: false });
    scene.add(new T.Points(wGeo, wMat));
    const tmp = new T.Color();
    const palAt = t => {
      t = ((t % 1) + 1) % 1;
      const f = t * (PAL.length), i0 = Math.floor(f) % PAL.length, i1 = (i0 + 1) % PAL.length;
      return tmp.copy(PAL[i0]).lerp(PAL[i1], f - Math.floor(f));
    };

    const sphereGroup = new T.Group();
    const SN = mob ? 900 : 2000, SR = 9;
    const sPos = new Float32Array(SN * 3), sCol = new Float32Array(SN * 3);
    const GA = Math.PI * (3 - Math.sqrt(5));
    const cA = new T.Color('#8fe9ff'), cB = new T.Color('#b46bff'), cC = new T.Color('#ff6bd6');
    for (let i = 0; i < SN; i++) {
      const y = 1 - (i / (SN - 1)) * 2, r = Math.sqrt(1 - y * y), th = GA * i;
      sPos.set([Math.cos(th) * r * SR, y * SR, Math.sin(th) * r * SR], i * 3);
      const t = (y + 1) / 2;
      const c = t < 0.5 ? tmp.copy(cC).lerp(cB, t * 2) : tmp.copy(cB).lerp(cA, (t - 0.5) * 2);
      sCol.set([c.r, c.g, c.b], i * 3);
    }
    const sGeo = new T.BufferGeometry();
    sGeo.setAttribute('position', new T.BufferAttribute(sPos, 3));
    sGeo.setAttribute('color', new T.BufferAttribute(sCol, 3));
    sphereGroup.add(new T.Points(sGeo, new T.PointsMaterial({ size: 0.32, map: texHard, vertexColors: true, transparent: true, opacity: 0.95, blending: T.AdditiveBlending, depthWrite: false })));
    const core = new T.Sprite(new T.SpriteMaterial({ map: texSoft, color: 0x8fe9ff, transparent: true, opacity: 0.55, blending: T.AdditiveBlending, depthWrite: false }));
    core.scale.set(9, 9, 1); sphereGroup.add(core);
    const RN = 240, rPos = new Float32Array(RN * 3), rCol = new Float32Array(RN * 3);
    for (let i = 0; i < RN; i++) {
      const a = (i / RN) * Math.PI * 2;
      rPos.set([Math.cos(a) * 13.5, 0, Math.sin(a) * 13.5], i * 3);
      const c = palAt(i / RN); rCol.set([c.r, c.g, c.b], i * 3);
    }
    const rGeo = new T.BufferGeometry();
    rGeo.setAttribute('position', new T.BufferAttribute(rPos, 3));
    rGeo.setAttribute('color', new T.BufferAttribute(rCol, 3));
    const ring = new T.Points(rGeo, new T.PointsMaterial({ size: 0.26, map: texHard, vertexColors: true, transparent: true, opacity: 0.8, blending: T.AdditiveBlending, depthWrite: false }));
    const ringGroup = new T.Group(); ringGroup.add(ring);
    ringGroup.rotation.set(1.05, 0, 0.35);
    sphereGroup.add(ringGroup);
    sphereGroup.position.set(mob ? 0 : 26, 3, -8);
    if (mob) sphereGroup.scale.setScalar(0.7);
    if (window.ARINA_HIDE_SPHERE) sphereGroup.visible = false; // ядро в центре — своё
    scene.add(sphereGroup);

    let mx = 0, my = 0, smx = 0, smy = 0, scrollT = 0;
    const bursts = [];
    let corePulse = 0;
    this._sphereTint = null;
    const ray = new T.Raycaster(), ndc = new T.Vector2(), plane = new T.Plane(new T.Vector3(0, 1, 0), 26), hit = new T.Vector3(0, -26, 0);
    const onMove = e => { mx = (e.clientX / innerWidth) * 2 - 1; my = -(e.clientY / innerHeight) * 2 + 1; };
    const onScroll = () => { scrollT = Math.min(scrollY, 900) / 900; };
    const onResize = () => {
      camera.aspect = innerWidth / innerHeight; camera.updateProjectionMatrix();
      renderer.setSize(innerWidth, innerHeight);
    };
    addEventListener('mousemove', onMove);
    addEventListener('scroll', onScroll, { passive: true });
    addEventListener('resize', onResize);

    const onClick = e => {
      if (e.target.closest && e.target.closest('a,button')) return;
      const nx = (e.clientX / innerWidth) * 2 - 1, ny = -(e.clientY / innerHeight) * 2 + 1;
      const sp = sphereGroup.position.clone().project(camera);
      if (Math.hypot(nx - sp.x, ny - sp.y) < 0.22) { corePulse = 1; return; }
      ray.setFromCamera(new T.Vector2(nx, ny), camera);
      const h = new T.Vector3();
      if (ray.ray.intersectPlane(plane, h)) {
        bursts.push({ x: h.x, z: h.z, t0: t });
        if (bursts.length > 5) bursts.shift();
      }
    };
    addEventListener('click', onClick);

    const clock = new T.Clock();
    let t = 0;
    const loop = () => {
      requestAnimationFrame(loop);
      const dt = Math.min(clock.getDelta(), 0.05) * this._motion;
      t += dt;
      smx += (mx - smx) * 0.05; smy += (my - smy) * 0.05;

      starLayers.forEach((p, k) => {
        p.rotation.y += p.userData.speed * dt * 8;
        p.rotation.x = smy * 0.05 * (k + 1);
        p.rotation.z = -smx * 0.03 * (k + 1);
      });

      nebulae.forEach(s => {
        const u = s.userData;
        s.position.x += Math.sin(t * 0.1 + u.ph) * u.dx * dt * 4;
        s.position.y += Math.cos(t * 0.08 + u.ph) * u.dy * dt * 4;
        s.material.opacity = u.base + Math.sin(t * 0.5 + u.ph) * 0.035;
      });

      ndc.set(smx, smy);
      ray.setFromCamera(ndc, camera);
      ray.ray.intersectPlane(plane, hit);

      const pa = wGeo.attributes.position.array, ca = wGeo.attributes.color.array;
      for (let k = 0; k < N; k++) {
        const x = pa[k * 3], z = pa[k * 3 + 2];
        let y = Math.sin(x * 0.07 + t * 1.1) * 1.5 + Math.cos(z * 0.09 + t * 0.8) * 1.2;
        const dx = x - hit.x, dz = z - hit.z, d = Math.sqrt(dx * dx + dz * dz);
        y += Math.sin(d * 0.5 - t * 6) * 3.4 * Math.exp(-d * 0.07);
        for (let bi = 0; bi < bursts.length; bi++) {
          const b = bursts[bi], age = t - b.t0;
          if (age > 3) continue;
          const bd = Math.sqrt((x - b.x) * (x - b.x) + (z - b.z) * (z - b.z));
          const rr = age * 34;
          y += Math.exp(-Math.abs(bd - rr) * 0.18) * 6 * (1 - age / 3);
        }
        pa[k * 3 + 1] = -26 + y;
        const b = 0.28 + Math.max(0, y + 2.7) * 0.11;
        const c = palAt(x / 220 + z / 300 + t * 0.03);
        ca[k * 3] = c.r * b; ca[k * 3 + 1] = c.g * b; ca[k * 3 + 2] = c.b * b;
      }
      wGeo.attributes.position.needsUpdate = true;
      wGeo.attributes.color.needsUpdate = true;

      sphereGroup.rotation.y += dt * 0.28;
      sphereGroup.rotation.x = smy * 0.25;
      sphereGroup.rotation.z = -smx * 0.12;
      sphereGroup.position.y = 3 + Math.sin(t * 0.7) * 1.6;
      const br = 1 + Math.sin(t * 1.4) * 0.035;
      sphereGroup.scale.setScalar((mob ? 0.7 : 1) * br);
      if (corePulse > 0) corePulse = Math.max(0, corePulse - dt * 1.2);
      core.material.opacity = 0.45 + Math.sin(t * 2.1) * 0.18 + corePulse * 0.4;
      core.scale.setScalar(8.5 + Math.sin(t * 2.1) * 1.4 + corePulse * 7);
      ringGroup.rotation.y += dt * (0.5 + corePulse * 3);
      const tint = this._sphereTint;
      const sc2 = sGeo.attributes.color.array;
      for (let i = 0; i < SN; i++) {
        const yy = sPos[i * 3 + 1] / SR, tt = (yy + 1) / 2;
        const base = tt < 0.5 ? tmp.copy(cC).lerp(cB, tt * 2) : tmp.copy(cB).lerp(cA, (tt - 0.5) * 2);
        if (tint) base.lerp(tint, 0.65);
        sc2[i * 3] += (base.r - sc2[i * 3]) * 0.06;
        sc2[i * 3 + 1] += (base.g - sc2[i * 3 + 1]) * 0.06;
        sc2[i * 3 + 2] += (base.b - sc2[i * 3 + 2]) * 0.06;
      }
      sGeo.attributes.color.needsUpdate = true;

      camera.position.x += (smx * 3.5 - camera.position.x) * 0.04;
      camera.position.y += (smy * 2 - camera.position.y) * 0.04;
      camera.position.z += ((62 + scrollT * 10) - camera.position.z) * 0.04;
      camera.lookAt(0, 0, 0);
      renderer.render(scene, camera);
    };
    loop();
  }
}

document.addEventListener('DOMContentLoaded', () => new ARINACore());
