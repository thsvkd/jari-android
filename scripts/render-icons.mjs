#!/usr/bin/env node
// 앱 안의 로고(홈 왼쪽 위 brand-mark: 짙은 둥근 네모 속 좌석 둘과 바닥선)로 앱 아이콘·스플래시·웹 아이콘·Play 스토어 그림을 만들어요.
// 이미지 도구 없이 저장소의 Playwright(Chromium)로 그려요. 로고를 바꾸면 아래 MARK 와 색만 고치고 다시 돌려요.
//
//   node scripts/render-icons.mjs
//
// 만드는 것
//   android/app/src/main/res/drawable/ic_launcher_foreground.xml   적응형 아이콘 앞면(벡터, 66dp 안전 영역 안)
//   android/app/src/main/res/drawable/ic_launcher_monochrome.xml   Android 13+ 테마 아이콘(벡터, 한 색)
//   android/app/src/main/res/values/ic_launcher_background.xml     적응형 아이콘 바탕색
//   android/app/src/main/res/mipmap-*/ic_launcher{,_round}.png      적응형 아이콘을 모르는 기기용
//   android/app/src/main/res/drawable*/splash.png                  런치 화면(크기는 원래 파일 그대로)
//   public/icon.svg                                                웹·매니페스트 아이콘
//   store/icon-512.png, store/feature-graphic.png                  Play 스토어 그림

import { chromium } from "@playwright/test";
import { readFileSync, readdirSync, writeFileSync, mkdirSync } from "node:fs";
import { join, resolve } from "node:path";

const ROOT = resolve(import.meta.dirname, "..");
const RES = join(ROOT, "android/app/src/main/res");

// src/styles.css 의 --text / --bg (라이트 테마): 로고 네모는 글자색, 그 안의 그림은 바탕색이에요.
const TILE = "#202631";
const INK = "#F6F7F9";
// src/app.ts 의 brand-mark SVG(viewBox 0 0 32 32)를 그대로 옮겼어요.
const MARK = {
  seats: [
    { x: 5, y: 7, w: 9, h: 14, r: 3 },
    { x: 18, y: 7, w: 9, h: 14, r: 3 },
  ],
  floor: { x1: 7, x2: 25, y: 25, width: 2.5 },
};
// 앱에서는 38px 네모 안에 25px 로 그려요. 아이콘도 네모 대비 같은 비율로 그려 같은 로고로 보이게 해요.
const MARK_IN_TILE = 25 / 38;
const TILE_RADIUS = 13 / 38;

const seatPath = ({ x, y, w, h, r }) =>
  `M${x + r},${y}H${x + w - r}A${r},${r} 0 0 1 ${x + w},${y + r}V${y + h - r}A${r},${r} 0 0 1 ${x + w - r},${y + h}H${x + r}A${r},${r} 0 0 1 ${x},${y + h - r}V${y + r}A${r},${r} 0 0 1 ${x + r},${y}Z`;
const floorPath = `M${MARK.floor.x1},${MARK.floor.y}H${MARK.floor.x2}`;

/** 32 단위 로고를 (cx, cy) 가운데에 size 크기로 그리는 SVG 조각. */
function markSvg(cx, cy, size, color) {
  const scale = size / 32;
  return `<g transform="translate(${cx - 16 * scale} ${cy - 16 * scale}) scale(${scale})">${MARK.seats
    .map((seat) => `<path d="${seatPath(seat)}" fill="${color}"/>`)
    .join("")}<path d="${floorPath}" fill="none" stroke="${color}" stroke-width="${MARK.floor.width}" stroke-linecap="round"/></g>`;
}

/** 적응형 아이콘(108dp)의 벡터. 앱에서처럼 보이는 72dp 가 네모라고 보고 로고를 그 비율로 두면 약 32dp 폭으로, 66dp 안전 영역 안이에요. */
function vectorDrawable(color) {
  const size = 72 * MARK_IN_TILE;
  const scale = size / 32;
  const offset = 54 - 16 * scale;
  return `<?xml version="1.0" encoding="utf-8"?>
<!-- scripts/render-icons.mjs 가 만든 파일이에요. 손으로 고치지 말고 스크립트를 고쳐 다시 만드세요. -->
<vector xmlns:android="http://schemas.android.com/apk/res/android"
    android:width="108dp"
    android:height="108dp"
    android:viewportWidth="108"
    android:viewportHeight="108">
    <group
        android:translateX="${offset.toFixed(3)}"
        android:translateY="${offset.toFixed(3)}"
        android:scaleX="${scale.toFixed(5)}"
        android:scaleY="${scale.toFixed(5)}">
${MARK.seats.map((seat) => `        <path android:fillColor="${color}" android:pathData="${seatPath(seat)}" />`).join("\n")}
        <path
            android:pathData="${floorPath}"
            android:strokeColor="${color}"
            android:strokeWidth="${MARK.floor.width}"
            android:strokeLineCap="round" />
    </group>
</vector>
`;
}

/** 네모(또는 원) 타일 위의 로고. inset 은 가장자리 여백(전체 대비). */
function tileSvg(size, { shape = "rounded", inset = 0 } = {}) {
  const side = size * (1 - 2 * inset);
  const origin = size * inset;
  const background =
    shape === "circle"
      ? `<circle cx="${size / 2}" cy="${size / 2}" r="${side / 2}" fill="${TILE}"/>`
      : shape === "square"
        ? `<rect width="${size}" height="${size}" fill="${TILE}"/>`
        : `<rect x="${origin}" y="${origin}" width="${side}" height="${side}" rx="${side * TILE_RADIUS}" fill="${TILE}"/>`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${size} ${size}" width="${size}" height="${size}">${background}${markSvg(size / 2, size / 2, side * MARK_IN_TILE, INK)}</svg>`;
}

const pngSize = (file) => {
  const data = readFileSync(file);
  return { width: data.readUInt32BE(16), height: data.readUInt32BE(20) };
};

async function render(page, html, width, height, file, { transparent = true, rgba = false } = {}) {
  await page.setViewportSize({ width, height });
  await page.setContent(`<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;background:transparent}body>svg,body>div{display:block}</style></head><body>${html}</body></html>`);
  await page.evaluate(() => document.fonts.ready);
  const shot = await page.screenshot({ omitBackground: transparent, clip: { x: 0, y: 0, width, height } });
  // Chromium 은 모두 불투명한 스크린샷을 알파 없는 PNG 로 줘요. Play 아이콘은 32비트 PNG 여야 해서 캔버스로 다시 써요(캔버스 PNG 는 늘 RGBA).
  const png = rgba
    ? Buffer.from(
        await page.evaluate(async (data) => {
          const image = new Image();
          image.src = `data:image/png;base64,${data}`;
          await image.decode();
          const canvas = document.createElement("canvas");
          canvas.width = image.width;
          canvas.height = image.height;
          canvas.getContext("2d").drawImage(image, 0, 0);
          return canvas.toDataURL("image/png").split(",")[1];
        }, shot.toString("base64")),
        "base64",
      )
    : shot;
  writeFileSync(file, png);
}

const browser = await chromium.launch();
const page = await browser.newPage({ deviceScaleFactor: 1 });
try {
  // 적응형 아이콘(API 26+): 벡터 앞면·한 색 레이어·바탕색.
  writeFileSync(join(RES, "drawable/ic_launcher_foreground.xml"), vectorDrawable(INK));
  writeFileSync(join(RES, "drawable/ic_launcher_monochrome.xml"), vectorDrawable("#FFFFFFFF"));
  writeFileSync(
    join(RES, "values/ic_launcher_background.xml"),
    `<?xml version="1.0" encoding="utf-8"?>\n<!-- scripts/render-icons.mjs -->\n<resources>\n    <color name="ic_launcher_background">${TILE}</color>\n</resources>\n`,
  );

  // 적응형 아이콘을 모르는 런처(API 25 이하)용 PNG. 48dp 기준, 가장자리 1/24 여백.
  for (const [density, scale] of Object.entries({ mdpi: 1, hdpi: 1.5, xhdpi: 2, xxhdpi: 3, xxxhdpi: 4 })) {
    const size = 48 * scale;
    await render(page, tileSvg(size, { inset: 1 / 24 }), size, size, join(RES, `mipmap-${density}/ic_launcher.png`));
    await render(page, tileSvg(size, { shape: "circle", inset: 1 / 24 }), size, size, join(RES, `mipmap-${density}/ic_launcher_round.png`));
  }

  // 런치 화면: 앱 바탕색 위 가운데에 로고 타일. 원래 파일의 크기를 그대로 써요.
  for (const dir of readdirSync(RES).filter((name) => name.startsWith("drawable"))) {
    const file = join(RES, dir, "splash.png");
    let size;
    try {
      size = pngSize(file);
    } catch {
      continue;
    }
    const tile = Math.round(Math.min(size.width, size.height) * 0.22);
    const html = `<div style="width:${size.width}px;height:${size.height}px;background:${INK};display:grid;place-items:center">${tileSvg(tile)}</div>`;
    await render(page, html, size.width, size.height, file, { transparent: false });
  }

  // 웹·매니페스트 아이콘("any maskable"): 원형 마스크의 안전 영역(가운데 80%) 안에 로고가 들어와요.
  writeFileSync(
    join(ROOT, "public/icon.svg"),
    `${tileSvg(512).replace("<svg ", '<svg role="img" aria-label="자리났다" ')}\n`,
  );

  // Play 스토어: 아이콘은 가장자리까지 채운 네모(Play 가 모양을 씌워요), 대표 그림은 알파 없이.
  mkdirSync(join(ROOT, "store"), { recursive: true });
  await render(page, tileSvg(512, { shape: "square" }), 512, 512, join(ROOT, "store/icon-512.png"), { rgba: true });
  const feature = `<div style="width:1024px;height:500px;background:${TILE};display:flex;align-items:center;gap:56px;padding:0 96px;box-sizing:border-box;font-family:'Noto Sans KR','Apple SD Gothic Neo',sans-serif;color:${INK}">
    <div style="flex:none;width:208px;height:208px;border-radius:${208 * TILE_RADIUS}px;background:${INK};display:grid;place-items:center">
      <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 208 208" width="208" height="208">${markSvg(104, 104, 208 * MARK_IN_TILE, TILE)}</svg>
    </div>
    <div style="display:grid;gap:18px">
      <div style="font-size:92px;font-weight:800;letter-spacing:-4px;line-height:1">자리났다</div>
      <div style="font-size:34px;font-weight:600;letter-spacing:-1px;line-height:1.35;color:#c2cbff">놓친 기차표, 빈자리가 나면<br>대신 잡아 드려요</div>
    </div>
  </div>`;
  await render(page, feature, 1024, 500, join(ROOT, "store/feature-graphic.png"), { transparent: false });
} finally {
  await browser.close();
}
console.log("아이콘·스플래시·스토어 그림을 다시 만들었어요.");
