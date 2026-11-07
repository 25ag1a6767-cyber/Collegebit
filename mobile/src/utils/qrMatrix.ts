
interface QrVersionSpec {
  size: number;
  dataBytes: number;
  ecBytes: number;
  align: number[];
}

const VERSIONS: Array<QrVersionSpec | null> = [
  null,
  { size: 21, dataBytes: 19, ecBytes: 7, align: [] },
  { size: 25, dataBytes: 34, ecBytes: 10, align: [6, 18] },
  { size: 29, dataBytes: 55, ecBytes: 15, align: [6, 22] },
  { size: 33, dataBytes: 80, ecBytes: 20, align: [6, 26] },
  { size: 37, dataBytes: 108, ecBytes: 26, align: [6, 30] }
];

function gfMul(a: number, b: number): number {
  let p = 0;
  let aa = a;
  let bb = b;
  for (let i = 0; i < 8; i++) {
    if (bb & 1) p ^= aa;
    const hi = aa & 0x80;
    aa = (aa << 1) & 0xff;
    if (hi) aa ^= 0x11d;
    bb >>= 1;
  }
  return p;
}

function rsRemainder(data: number[], ecLen: number): number[] {
  let poly = [1];
  let root = 1;
  for (let i = 0; i < ecLen; i++) {
    const next = new Array<number>(poly.length + 1).fill(0);
    for (let j = 0; j < poly.length; j++) {
      next[j] ^= poly[j];
      next[j + 1] ^= gfMul(poly[j], root);
    }
    poly = next;
    root = gfMul(root, 2);
  }
  const rem = new Array<number>(ecLen).fill(0);
  for (const byte of data) {
    const factor = byte ^ rem[0];
    rem.shift();
    rem.push(0);
    for (let i = 0; i < ecLen; i++) {
      rem[i] ^= gfMul(poly[i + 1], factor);
    }
  }
  return rem;
}

function encodeAsciiBytes(value: string): number[] {
  const out: number[] = [];
  for (let i = 0; i < value.length; i++) {
    out.push(value.charCodeAt(i) & 0xff);
  }
  return out;
}

export function buildQrMatrix(payload: string): boolean[][] {
  const bytes = encodeAsciiBytes(payload);
  let ver = 1;
  while (ver <= 5 && bytes.length + 2 > (VERSIONS[ver]?.dataBytes ?? 0)) ver++;
  if (ver > 5) {
    bytes.length = (VERSIONS[5]?.dataBytes ?? 108) - 2;
    ver = 5;
  }
  const spec = VERSIONS[ver]!;
  const bits: number[] = [];
  const push = (val: number, len: number) => {
    for (let i = len - 1; i >= 0; i--) bits.push((val >> i) & 1);
  };
  push(0b0100, 4);
  push(bytes.length, 8);
  for (const b of bytes) push(b, 8);
  push(0, Math.min(4, spec.dataBytes * 8 - bits.length));
  while (bits.length % 8 !== 0) bits.push(0);

  const codewords: number[] = [];
  for (let i = 0; i < bits.length; i += 8) {
    let cw = 0;
    for (let j = 0; j < 8; j++) cw = (cw << 1) | bits[i + j];
    codewords.push(cw);
  }
  let toggle = true;
  while (codewords.length < spec.dataBytes) {
    codewords.push(toggle ? 0xec : 0x11);
    toggle = !toggle;
  }
  const full = codewords.concat(rsRemainder(codewords, spec.ecBytes));

  const n = spec.size;
  const grid: boolean[][] = Array.from({ length: n }, () => new Array<boolean>(n).fill(false));
  const reserved: boolean[][] = Array.from({ length: n }, () => new Array<boolean>(n).fill(false));

  const setMod = (r: number, c: number, dark: boolean) => {
    if (r >= 0 && r < n && c >= 0 && c < n) {
      grid[r][c] = dark;
      reserved[r][c] = true;
    }
  };

  const finder = (top: number, left: number) => {
    for (let dr = -1; dr <= 7; dr++) {
      for (let dc = -1; dc <= 7; dc++) {
        const inside = dr >= 0 && dr <= 6 && dc >= 0 && dc <= 6;
        const ring = dr === 0 || dr === 6 || dc === 0 || dc === 6;
        const core = dr >= 2 && dr <= 4 && dc >= 2 && dc <= 4;
        setMod(top + dr, left + dc, inside && (ring || core));
      }
    }
  };
  finder(0, 0);
  finder(0, n - 7);
  finder(n - 7, 0);

  for (const r of spec.align) {
    for (const c of spec.align) {
      if (reserved[r][c]) continue;
      for (let dr = -2; dr <= 2; dr++) {
        for (let dc = -2; dc <= 2; dc++) {
          setMod(r + dr, c + dc, Math.max(Math.abs(dr), Math.abs(dc)) !== 1);
        }
      }
    }
  }
  for (let i = 8; i < n - 8; i++) {
    if (!reserved[6][i]) setMod(6, i, i % 2 === 0);
    if (!reserved[i][6]) setMod(i, 6, i % 2 === 0);
  }
  setMod(n - 8, 8, true);

  for (let i = 0; i < 9; i++) {
    reserved[8][i] = true;
    reserved[i][8] = true;
  }
  for (let i = 0; i < 8; i++) {
    reserved[8][n - 1 - i] = true;
    reserved[n - 1 - i][8] = true;
  }

  let bitIdx = 0;
  const maxBits = full.length * 8;
  let upward = true;
  for (let right = n - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let v = 0; v < n; v++) {
      const r = upward ? n - 1 - v : v;
      for (let j = 0; j < 2; j++) {
        const c = right - j;
        if (reserved[r][c]) continue;
        let dark = false;
        if (bitIdx < maxBits) {
          dark = ((full[bitIdx >> 3] >> (7 - (bitIdx & 7))) & 1) === 1;
          bitIdx++;
        }
        if ((r + c) % 2 === 0) dark = !dark;
        grid[r][c] = dark;
      }
    }
    upward = !upward;
  }

  const fmt = 0x77c4;
  for (let i = 0; i <= 5; i++) grid[8][i] = ((fmt >> (14 - i)) & 1) === 1;
  grid[8][7] = ((fmt >> 8) & 1) === 1;
  grid[8][8] = ((fmt >> 7) & 1) === 1;
  grid[7][8] = ((fmt >> 6) & 1) === 1;
  for (let i = 9; i < 15; i++) grid[14 - i][8] = ((fmt >> (14 - i)) & 1) === 1;
  for (let i = 0; i < 8; i++) grid[8][n - 1 - i] = ((fmt >> i) & 1) === 1;
  for (let i = 8; i < 15; i++) grid[n - 15 + i][8] = ((fmt >> i) & 1) === 1;

  return grid;
}
