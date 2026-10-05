"""EmailToMCP 앱 아이콘 생성 스크립트.

"이메일 자동화"를 상징하는 아이콘을 직접 그린다(외부 이미지 복제 없음, 원본 제작).
- 모티프: 편지봉투(이메일) + 스파크/별(자동화·AI 보조) 조합.
- 색상: docs/design/UI_디자인가이드.md의 primary-gradient(#5678ff -> #315aff, 280deg)를 그대로 쓴다.
- 산출물: app_icon_1024.png(마스터), app_icon.ico(Windows, 다중 해상도), app_icon.icns(macOS),
          app_icon_256.png / app_icon_512.png(기타 용도, 트레이 아이콘 등)

실행: (.venv) python packaging/icons/make_icon.py
의존성: pillow, icnsutil (이 스크립트 실행 전용 — 런타임 의존성 아님, pyproject에 추가하지 않음)
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

OUT_DIR = Path(__file__).parent

# docs/design/UI_디자인가이드.md 컬러 토큰
PRIMARY_START = (0x56, 0x78, 0xFF)  # #5678ff
PRIMARY_END = (0x31, 0x5A, 0xFF)  # #315aff
WHITE = (0xFF, 0xFF, 0xFF)
FAVORITE = (0xF7, 0xA4, 0x43)  # #f7a443 (강조 스파크)

SIZE = 1024


def _rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    d = ImageDraw.Draw(mask)
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def _diagonal_gradient(
    size: int, c1: tuple[int, int, int], c2: tuple[int, int, int], angle_deg: float
) -> Image.Image:
    """280도 선형 그라디언트(QSS의 qlineargradient(x1:0,y1:0,x2:1,y2:0.3)와 비슷한 느낌)."""
    base = Image.new("L", (size, size), 0)
    for y in range(size):
        for x in range(size):
            pass
    # 픽셀 루프 대신 1차원 그라디언트를 만들어 회전 확대하는 방식(빠르고 매끈함)
    grad = Image.linear_gradient("L").resize((size * 2, size * 2))
    grad = grad.rotate(angle_deg, resample=Image.BICUBIC)
    left = (grad.width - size) // 2
    grad = grad.crop((left, left, left + size, left + size))
    r = Image.new("L", (size, size))
    g = Image.new("L", (size, size))
    b = Image.new("L", (size, size))
    # 그레이스케일 그라디언트를 두 색 사이로 보간
    px = grad.load()
    rpx, gpx, bpx = r.load(), g.load(), b.load()
    for y in range(size):
        for x in range(size):
            t = px[x, y] / 255.0
            rpx[x, y] = int(c1[0] + (c2[0] - c1[0]) * t)
            gpx[x, y] = int(c1[1] + (c2[1] - c1[1]) * t)
            bpx[x, y] = int(c1[2] + (c2[2] - c1[2]) * t)
    return Image.merge("RGB", (r, g, b))


def _envelope(size: int) -> Image.Image:
    """흰색 편지봉투 실루엣(테두리 + 봉투 접힘선)을 그린 레이어."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    w, h = size * 0.56, size * 0.38
    x0, y0 = (size - w) / 2, (size - h) / 2 + size * 0.03
    x1, y1 = x0 + w, y0 + h
    corner = h * 0.14

    # 봉투 몸통(둥근 사각형)
    d.rounded_rectangle([x0, y0, x1, y1], radius=corner, fill=WHITE)

    # 봉투 뚜껑(삼각형, 살짝 어두운 흰색으로 입체감)
    flap_color = (0xF0, 0xF3, 0xFF)
    d.polygon(
        [
            (x0 + corner * 0.6, y0 + corner * 0.4),
            (size / 2, y0 + h * 0.52),
            (x1 - corner * 0.6, y0 + corner * 0.4),
        ],
        fill=flap_color,
    )
    # 뚜껑 테두리선
    d.line(
        [(x0 + corner * 0.6, y0 + corner * 0.4), (size / 2, y0 + h * 0.52)],
        fill=(0xC9, 0xD3, 0xF2),
        width=max(2, int(size * 0.004)),
    )
    d.line(
        [(size / 2, y0 + h * 0.52), (x1 - corner * 0.6, y0 + corner * 0.4)],
        fill=(0xC9, 0xD3, 0xF2),
        width=max(2, int(size * 0.004)),
    )
    return layer


def _spark(size: int, cx: float, cy: float, r: float, color: tuple[int, int, int]) -> Image.Image:
    """네 꼭짓점 별(스파크) 모양 — 자동화/AI 보조를 상징."""
    layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    points = []
    for i in range(8):
        ang = math.pi / 4 * i
        rad = r if i % 2 == 0 else r * 0.38
        points.append((cx + rad * math.cos(ang), cy + rad * math.sin(ang)))
    d.polygon(points, fill=color)
    # 작은 보조 스파크
    d.ellipse(
        [
            cx + r * 1.15 - r * 0.12,
            cy - r * 1.15 - r * 0.12,
            cx + r * 1.15 + r * 0.12,
            cy - r * 1.15 + r * 0.12,
        ],
        fill=color,
    )
    return layer


def build_master() -> Image.Image:
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    radius = int(SIZE * 0.22)  # macOS 스타일 둥근 사각형
    bg = _diagonal_gradient(SIZE, PRIMARY_START, PRIMARY_END, angle_deg=-20).convert("RGBA")
    mask = _rounded_mask(SIZE, radius)
    canvas.paste(bg, (0, 0), mask)

    # 은은한 하단 그림자(살짝 어둡게)로 입체감 — 배경 위에 반투명 검정을 소량만 얹음
    shade = Image.new("L", (SIZE, SIZE), 0)
    sd = ImageDraw.Draw(shade)
    sd.rectangle([0, int(SIZE * 0.62), SIZE, SIZE], fill=70)
    shade = shade.filter(ImageFilter.GaussianBlur(SIZE * 0.08))
    dark_overlay = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    dark_overlay.putalpha(shade)
    canvas.alpha_composite(dark_overlay)
    canvas.putalpha(mask)

    envelope = _envelope(SIZE)
    canvas.alpha_composite(envelope)

    spark = _spark(SIZE, cx=SIZE * 0.775, cy=SIZE * 0.305, r=SIZE * 0.085, color=FAVORITE)
    canvas.alpha_composite(spark)

    # 바깥 둥근 모서리 다시 한 번 깨끗하게 마스킹
    canvas.putalpha(mask)
    return canvas


def main() -> None:
    master = build_master()
    master.save(OUT_DIR / "app_icon_1024.png")

    for sz in (16, 32, 48, 64, 128, 256, 512):
        master.resize((sz, sz), Image.LANCZOS).save(OUT_DIR / f"app_icon_{sz}.png")

    # Windows .ico (다중 해상도 포함)
    ico_sizes = [16, 32, 48, 64, 128, 256]
    master.save(
        OUT_DIR / "app_icon.ico",
        format="ICO",
        sizes=[(s, s) for s in ico_sizes],
    )

    # macOS .icns (icnsutil, macOS 없이도 생성 가능)
    try:
        import icnsutil

        writer = icnsutil.IcnsFile()
        size_to_key = {
            16: "icp4",
            32: "icp5",
            64: "icp6",
            128: "ic07",
            256: "ic08",
            512: "ic09",
            1024: "ic10",
        }
        for sz, key in size_to_key.items():
            png_path = OUT_DIR / f"app_icon_{sz}.png"
            if not png_path.exists():
                master.resize((sz, sz), Image.LANCZOS).save(png_path)
            writer.add_media(key, file=str(png_path))
        writer.write(str(OUT_DIR / "app_icon.icns"))
        print("app_icon.icns 생성 완료")
    except Exception as exc:  # noqa: BLE001 - 생성 스크립트이므로 실패해도 전체를 막지 않음
        print(f"icns 생성 실패(무시 가능, macOS에서 iconutil로 재생성 권장): {exc}")

    print("아이콘 생성 완료:", OUT_DIR)


if __name__ == "__main__":
    main()
