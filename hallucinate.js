// ═══════════════════════════════════════════════════════════════════
//  HALLUCINATION ENGINE  –  10 Optical Illusions
//  p5.js sketch  ·  Each effect exploits a different perceptual hack
// ═══════════════════════════════════════════════════════════════════

let currentIllusion = null;
let t = 0;
let cx, cy, maxR;

// ── Per-illusion metadata ────────────────────────────────────────
const illusionData = {
  spiral:       { name: 'Spiral Vortex',       tip: 'Stare at the center for 30 s, then look at a wall or your hand.' },
  rings:        { name: 'Pulsing Rings',       tip: 'Focus on the dot. After 30 s, look at a textured surface.' },
  snakes:       { name: 'Rotating Snakes',     tip: 'Focus on the center. The rings appear to rotate on their own.' },
  phantom:      { name: 'Phantom Grid',        tip: 'Look at the center. Gray ghost dots appear at the intersections.' },
  moire:        { name: 'Moiré Interference',  tip: 'Watch the center. Impossible ripples emerge from simple circles.' },
  tunnel:       { name: 'Infinite Tunnel',     tip: 'Stare at the center. Feel yourself falling forward into the void.' },
  colorburn:    { name: 'Color Burn',          tip: 'Stare at the center for 60 s. Then look at a white wall.' },
  scintillate:  { name: 'Scintillating Grid',  tip: 'Focus on the center. White dots in your periphery flash dark.' },
  liquid:       { name: 'Liquid Surface',      tip: 'Focus on the center. The surface appears to flow and breathe.' },
  kaleidoscope: { name: 'Kaleidoscope Melt',   tip: 'Stare at the center for 30 s. The edges of your vision dissolve.' },
};

// ─── p5.js  SETUP / DRAW ────────────────────────────────────────

function setup() {
  let canvas = createCanvas(windowWidth, windowHeight);
  canvas.parent('canvas-container');
  colorMode(HSB, 360, 100, 100, 100);
  updateDims();
  noLoop();
}

function draw() {
  if (!currentIllusion) return;
  updateDims();
  t += deltaTime * 0.001;

  switch (currentIllusion) {
    case 'spiral':       drawSpiralVortex();      break;
    case 'rings':        drawPulsingRings();       break;
    case 'snakes':       drawRotatingSnakes();     break;
    case 'phantom':      drawPhantomGrid();        break;
    case 'moire':        drawMoireWaves();         break;
    case 'tunnel':       drawInfiniteTunnel();     break;
    case 'colorburn':    drawColorBurn();          break;
    case 'scintillate':  drawScintillatingGrid();  break;
    case 'liquid':       drawLiquidSurface();      break;
    case 'kaleidoscope': drawKaleidoscopeMelt();   break;
  }
}

function updateDims() {
  cx = width  / 2;
  cy = height / 2;
  maxR = min(width, height) * 0.48;
}

// ─── UI  INTERACTIONS ───────────────────────────────────────────

function selectIllusion(id) {
  currentIllusion = id;
  t = 0;

  document.body.classList.add('illusion-active');
  document.getElementById('menu').classList.add('hidden');

  setTimeout(() => {
    document.getElementById('canvas-container').classList.add('visible');
    document.getElementById('back-btn').classList.add('visible');

    const nameEl = document.getElementById('illusion-name');
    nameEl.textContent = illusionData[id].name;
    nameEl.classList.add('visible');

    const instrEl = document.getElementById('instructions');
    instrEl.textContent = illusionData[id].tip;
    instrEl.classList.remove('fade');
    instrEl.classList.add('visible');
  }, 350);

  resizeCanvas(windowWidth, windowHeight);
  noCursor();
  loop();

  // Auto-fade instructions after 7 s
  setTimeout(() => {
    document.getElementById('instructions').classList.add('fade');
  }, 7500);
}

function goBack() {
  currentIllusion = null;
  noLoop();
  cursor();

  document.body.classList.remove('illusion-active');
  document.getElementById('canvas-container').classList.remove('visible');
  document.getElementById('back-btn').classList.remove('visible');
  document.getElementById('illusion-name').classList.remove('visible');
  const instrEl = document.getElementById('instructions');
  instrEl.classList.remove('visible', 'fade');

  setTimeout(() => {
    document.getElementById('menu').classList.remove('hidden');
  }, 300);
}

function windowResized() {
  resizeCanvas(windowWidth, windowHeight);
  updateDims();
}

function keyPressed() {
  if (keyCode === ESCAPE && currentIllusion) { goBack(); return; }

  if (!currentIllusion) {
    const map = {
      49:'spiral', 50:'rings', 51:'snakes', 52:'phantom', 53:'moire',
      54:'tunnel', 55:'colorburn', 56:'scintillate', 57:'liquid', 48:'kaleidoscope'
    };
    if (map[keyCode]) selectIllusion(map[keyCode]);
  }
}

// ─── HELPERS ────────────────────────────────────────────────────

/** Pulsing red fixation dot (call while translated to center). */
function drawFixationDot() {
  noStroke();
  fill(0, 0, 100, 12);
  let g = 22 + 5 * sin(t * 2);
  ellipse(0, 0, g, g);

  fill(0, 85, 100, 88);
  let c = 9 + 2 * sin(t * 3);
  ellipse(0, 0, c, c);

  fill(0, 0, 100);
  ellipse(0, 0, 3, 3);
}

/** Fixation dot at screen center (no prior translate needed). */
function drawCenterDot() {
  push(); translate(cx, cy); drawFixationDot(); pop();
}

/** Filled donut-arc segment between two radii and two angles. */
function arcSeg(ri, ro, a1, a2) {
  let step = max(0.04, (a2 - a1) / 18);
  beginShape();
  for (let a = a1; a <= a2 + 0.001; a += step) vertex(cos(a)*ro, sin(a)*ro);
  for (let a = a2; a >= a1 - 0.001; a -= step) vertex(cos(a)*ri, sin(a)*ri);
  endShape(CLOSE);
}


// ═══════════════════════════════════════════════════════════════════
//  1 ·  SPIRAL VORTEX
//  Rotating logarithmic spiral with B&W arms.
//  Triggers the motion aftereffect (waterfall illusion).
// ═══════════════════════════════════════════════════════════════════

function drawSpiralVortex() {
  background(0);
  push();
  translate(cx, cy);

  const ARMS = 12, STEPS = 500, rot = t * 1.3;

  for (let arm = 0; arm < ARMS; arm++) {
    let off = (TWO_PI / ARMS) * arm;
    noStroke();
    fill(arm % 2 === 0 ? color(0,0,100,55) : color(0,0,0,85));

    let p1 = [], p2 = [];
    for (let s = 0; s <= STEPS; s++) {
      let f = s / STEPS, r = f * maxR;
      let a1 = off + log(1 + r * 0.015) * 8 + rot;
      p1.push([cos(a1)*r, sin(a1)*r]);
      let a2 = off + PI/ARMS + log(1 + r*0.015)*8 + rot;
      p2.push([cos(a2)*r, sin(a2)*r]);
    }

    beginShape();
    for (let p of p1) vertex(p[0], p[1]);
    for (let i = p2.length-1; i >= 0; i--) vertex(p2[i][0], p2[i][1]);
    endShape(CLOSE);
  }

  // Counter-rotating thin spiral (amplifies perceptual conflict)
  const MICRO = 8, MS = 350, mRot = -t * 0.7, mMax = maxR * 0.55;
  stroke(0,0,100,18); strokeWeight(1.4); noFill();
  for (let a = 0; a < MICRO; a++) {
    let off = (TWO_PI / MICRO) * a;
    beginShape();
    for (let s = 0; s <= MS; s++) {
      let f = s/MS, r = f*mMax;
      let ang = off + log(1+r)*3 + mRot;
      vertex(cos(ang)*r, sin(ang)*r);
    }
    endShape();
  }

  // Colour overlay for chromatic fatigue
  noStroke();
  for (let i = 0; i < 5; i++) {
    let r = maxR*(i+1)/6, h = (t*20+i*72)%360;
    fill(h,40,50,5);
    ellipse(0,0,r*2,r*2);
  }

  drawFixationDot();
  pop();
}


// ═══════════════════════════════════════════════════════════════════
//  2 ·  PULSING RINGS
//  Concentric B&W rings continuously expanding outward.
//  Creates radial motion aftereffect.
// ═══════════════════════════════════════════════════════════════════

function drawPulsingRings() {
  background(0);
  push();
  translate(cx, cy);

  const N = 55, speed = t * 0.4, thick = maxR / N;
  noFill();
  for (let i = 0; i < N; i++) {
    let phase = (i / N + speed) % 1.0;
    let r = phase * maxR;
    strokeWeight(thick * 0.93);
    stroke(0, 0, i % 2 === 0 ? 100 : 0, 80);
    ellipse(0, 0, r*2, r*2);
  }

  // Subtle hue tint
  noStroke();
  for (let i = 0; i < 4; i++) {
    let r = maxR*(i+1)/5, h = (t*25+i*90)%360;
    fill(h,35,45,4);
    ellipse(0,0,r*2,r*2);
  }

  drawFixationDot();
  pop();
}


// ═══════════════════════════════════════════════════════════════════
//  3 ·  ROTATING SNAKES
//  Concentric rings of four-colour segments whose asymmetric
//  luminance gradient makes stationary rings appear to rotate.
//  Inspired by Akiyoshi Kitaoka's illusion.
// ═══════════════════════════════════════════════════════════════════

function drawRotatingSnakes() {
  background(0, 0, 55);
  push();
  translate(cx, cy);

  const RINGS = 10, RW = maxR / (RINGS + 1), SEGS = 16;
  const segA = TWO_PI / SEGS;

  // Sequence: black → dark blue → white → yellow  (apparent CW)
  const cols = [
    [0,   0,   5],
    [220, 75, 35],
    [0,   0,  98],
    [50,  80, 88],
  ];

  for (let r = 0; r < RINGS; r++) {
    let outerR = (r + 1.5) * RW;
    let innerR = outerR - RW * 0.85;
    if (outerR > maxR) break;

    let shift = r % 2 === 0 ? 0 : 2;            // reverse perceived direction
    let breathe = sin(t * 0.45 + r * 0.35) * 0.003; // micro-drift

    for (let s = 0; s < SEGS; s++) {
      let ci = (s + shift) % 4;
      fill(cols[ci][0], cols[ci][1], cols[ci][2]);
      noStroke();
      let a1 = s * segA + breathe * r;
      let a2 = a1 + segA * 0.96;
      arcSeg(innerR, outerR, a1, a2);
    }
  }

  drawFixationDot();
  pop();
}


// ═══════════════════════════════════════════════════════════════════
//  4 ·  PHANTOM GRID  (Hermann Grid)
//  Black squares with light gaps — phantom dark dots appear
//  at intersections in peripheral vision.
// ═══════════════════════════════════════════════════════════════════

function drawPhantomGrid() {
  let sq = 55, gap = 10 + sin(t * 0.28) * 1.2;
  let stride = sq + gap;

  // Gap colour (light gray)
  background(0, 0, 78);

  noStroke();
  fill(0, 0, 8);

  let cols = ceil(width / stride) + 2;
  let rows = ceil(height / stride) + 2;
  let ox = (width  - cols * stride) / 2;
  let oy = (height - rows * stride) / 2;

  for (let r = 0; r < rows; r++)
    for (let c = 0; c < cols; c++)
      rect(ox + c * stride, oy + r * stride, sq, sq, 2);

  drawCenterDot();
}


// ═══════════════════════════════════════════════════════════════════
//  5 ·  MOIRÉ INTERFERENCE
//  Two sets of concentric circles with a slowly drifting offset.
//  Their overlap creates organic rippling interference patterns.
// ═══════════════════════════════════════════════════════════════════

function drawMoireWaves() {
  background(0);

  let sp = 11;
  let maxC = int(max(width, height) / sp) + 5;

  strokeWeight(1.6);
  noFill();

  // Set A — centred
  stroke(0, 0, 100, 50);
  for (let i = 1; i < maxC; i++) ellipse(cx, cy, i*sp*2, i*sp*2);

  // Set B — orbiting offset
  let d = 22 + 12 * sin(t * 0.18);
  let bx = cx + d * cos(t * 0.22);
  let by = cy + d * sin(t * 0.22);

  stroke(0, 0, 100, 50);
  for (let i = 1; i < maxC; i++) ellipse(bx, by, i*sp*2, i*sp*2);

  drawCenterDot();
}


// ═══════════════════════════════════════════════════════════════════
//  6 ·  INFINITE TUNNEL
//  Alternating B&W circles expanding outward + rotating squares.
//  Creates depth vertigo and a sensation of forward motion.
// ═══════════════════════════════════════════════════════════════════

function drawInfiniteTunnel() {
  background(0);
  push();
  translate(cx, cy);

  // ── Concentric zoom circles ──
  const N = 32, speed = t * 0.35;
  let rings = [];
  for (let i = 0; i < N; i++) {
    let phase = ((i / N) + speed) % 1.0;
    rings.push(phase * maxR * 1.15);
  }
  rings.sort((a, b) => b - a); // draw largest first

  noStroke();
  for (let j = 0; j < rings.length; j++) {
    if (rings[j] < 1) continue;
    fill(0, 0, j % 2 === 0 ? 5 : 100);
    ellipse(0, 0, rings[j]*2, rings[j]*2);
  }

  // ── Rotating square wireframes on top ──
  rectMode(CENTER);
  let sqN = 18, sqSpeed = t * 0.3;
  for (let i = 0; i < sqN; i++) {
    let phase = ((i / sqN) + speed * 0.7) % 1.0;
    let sz = phase * maxR * 1.5;
    if (sz < 4 || sz > maxR * 2) continue;
    push();
    rotate(i * PI / 16 + sqSpeed);
    noFill();
    strokeWeight(1.2);
    stroke(0, 0, i % 2 === 0 ? 100 : 40, 20);
    rect(0, 0, sz, sz);
    pop();
  }

  drawFixationDot();
  pop();
}


// ═══════════════════════════════════════════════════════════════════
//  7 ·  COLOR BURN
//  Slowly rotating mandala of highly saturated colours.
//  Fatigues cone cells → vivid complementary afterimages.
// ═══════════════════════════════════════════════════════════════════

function drawColorBurn() {
  background(0);
  push();
  translate(cx, cy);
  noStroke();

  const PETALS = 6, RINGS = 6;

  for (let ring = RINGS; ring >= 1; ring--) {
    let oR = (ring / RINGS) * maxR;
    let iR = ((ring - 1) / RINGS) * maxR;

    for (let p = 0; p < PETALS; p++) {
      let h = (p * (360/PETALS) + ring * 60 + t * 8) % 360;
      fill(h, 95, 93, 90);
      let a1 = (p / PETALS) * TWO_PI;
      let a2 = ((p + 1) / PETALS) * TWO_PI;
      arcSeg(iR, oR, a1, a2);
    }
  }

  // Pulsing intensity overlay
  for (let i = 0; i < 4; i++) {
    let r = maxR * (i+1) / 5;
    let h = (t * 35 + i * 90) % 360;
    fill(h, 100, 100, 10 + 5 * sin(t * 1.5 + i));
    ellipse(0, 0, r*2, r*2);
  }

  drawFixationDot();
  pop();
}


// ═══════════════════════════════════════════════════════════════════
//  8 ·  SCINTILLATING GRID
//  Dark-gray grid on black with white dots at intersections.
//  Peripheral dots appear to flash dark (lateral inhibition).
// ═══════════════════════════════════════════════════════════════════

function drawScintillatingGrid() {
  background(0);

  let sq = 50, lw = 8 + sin(t * 0.22) * 0.5;
  let stride = sq + lw;

  let cols = ceil(width / stride) + 2;
  let rows = ceil(height / stride) + 2;
  let ox = (width  - cols * stride) / 2 + sq / 2;
  let oy = (height - rows * stride) / 2 + sq / 2;

  // Gray lines
  stroke(0, 0, 38);
  strokeWeight(lw);
  for (let r = 0; r <= rows; r++) {
    let y = oy + r * stride;
    line(0, y, width, y);
  }
  for (let c = 0; c <= cols; c++) {
    let x = ox + c * stride;
    line(x, 0, x, height);
  }

  // White dots at intersections
  noStroke();
  fill(0, 0, 100);
  let dot = lw * 1.3;
  for (let r = 0; r <= rows; r++)
    for (let c = 0; c <= cols; c++)
      ellipse(ox + c * stride, oy + r * stride, dot, dot);

  drawCenterDot();
}


// ═══════════════════════════════════════════════════════════════════
//  9 ·  LIQUID SURFACE
//  A dense grid of dots displaced by layered sine waves.
//  Creates the illusion of an organic, flowing 3-D surface.
// ═══════════════════════════════════════════════════════════════════

function drawLiquidSurface() {
  background(0, 0, 4);

  let sp = 20;
  let cols = floor(width  / sp) + 2;
  let rows = floor(height / sp) + 2;
  let ox = (width  - cols * sp) / 2;
  let oy = (height - rows * sp) / 2;

  noStroke();
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      let bx = ox + c * sp;
      let by = oy + r * sp;

      let dx = sin(by * 0.028 + t * 1.6) * 9
             + sin(bx * 0.018 + t * 0.75) * 5;
      let dy = cos(bx * 0.028 + t * 1.3) * 9
             + cos(by * 0.018 + t * 0.55) * 5;

      let disp = sqrt(dx*dx + dy*dy);
      let h = (disp * 14 + t * 28) % 360;
      let sz = 3.2 + disp * 0.28;

      fill(h, 55, 82, 80);
      ellipse(bx + dx, by + dy, sz, sz);
    }
  }

  drawCenterDot();
}


// ═══════════════════════════════════════════════════════════════════
// 10 ·  KALEIDOSCOPE MELT
//  6-fold mirrored symmetry with noise-driven organic blobs
//  and colour cycling.  Edges of perception dissolve.
// ═══════════════════════════════════════════════════════════════════

function drawKaleidoscopeMelt() {
  background(0);
  push();
  translate(cx, cy);

  const SEGS = 6, segA = TWO_PI / SEGS;

  for (let s = 0; s < SEGS; s++) {
    push();
    rotate(s * segA);
    if (s % 2 === 1) scale(1, -1);
    drawKaleidoSector(segA);
    pop();
  }

  drawFixationDot();
  pop();
}

function drawKaleidoSector(segAngle) {
  noStroke();
  for (let r = 18; r < maxR; r += 24) {
    let nPts = max(2, floor(r * segAngle / 26));
    for (let i = 0; i < nPts; i++) {
      let a = (i / nPts) * segAngle * 0.95;
      let x = cos(a) * r;
      let y = sin(a) * r;

      let nx = noise(x * 0.006 + t * 0.28, y * 0.006) * 16 - 8;
      let ny = noise(y * 0.006, x * 0.006 + t * 0.28) * 16 - 8;
      let n  = noise(x * 0.004, y * 0.004, t * 0.18);
      let h  = (n * 360 + t * 22) % 360;
      let sz = 6 + n * 13 + 3.5 * sin(t * 0.7 + r * 0.018);

      fill(h, 72, 90, 52);
      ellipse(x + nx, y + ny, sz, sz);
    }
  }
}
