"""
媒体库封面生成 - 多图旋转海报样式

根据 template.pen 设计稿实现：
- 1920×1080 画布，纯色渐变背景（从海报提取主色调）
- 右侧 3 列 × 3 行旋转 -15° 的海报网格
- 从上到下的半透明黑色渐变遮罩
- 左下角中英文双行标题

参考：https://github.com/justzerock/MoviePilot-Plugins/tree/main/plugins.v2/mediacovergenerator
"""

import base64
import colorsys
import io
import math
import os
import random
from collections import Counter
from pathlib import Path
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

from app.log import logger

# ============================================================
# 常量 —— 全部基于 template.pen 的 320×180 设计稿 × 6 换算
# ============================================================

SCALE = 6  # 设计稿 320×180 → 实际 1920×1080

CANVAS_WIDTH = 320 * SCALE   # 1920
CANVAS_HEIGHT = 180 * SCALE  # 1080

# 海报尺寸与间距
POSTER_WIDTH = 64 * SCALE     # 384
POSTER_HEIGHT = 96 * SCALE    # 576
POSTER_GAP = 4 * SCALE        # 24
POSTER_CORNER_RADIUS = 4 * SCALE  # 24

# 海报网格配置
GRID_ROWS = 3
GRID_COLS = 3
ROTATION_ANGLE = -15  # 旋转角度（度）

# 三列海报在容器内的起始位置 (x, y) × SCALE
# layout 计算后的实际坐标：
# 列 1: x=0, 图片从 y=32 开始 (padding-top=32)
# 列 2: x=68, 图片从 y=0 开始 (无 padding)
# 列 3: x=136, 图片从 y=32 开始 (padding-top=32)
COL_POSITIONS = [
    (0 * SCALE, 32 * SCALE),      # 第 1 列: padding-top=32
    (68 * SCALE, 0 * SCALE),      # 第 2 列: 无 padding
    (136 * SCALE, 32 * SCALE),    # 第 3 列: padding-top=32
]

# 海报网格容器（layout 计算后的实际尺寸和位置）
GRID_CONTAINER_W = 200 * SCALE      # 64*3 + 4*2 = 200（三列宽度 + 间距）
GRID_CONTAINER_H = 328 * SCALE      # 最高列的高度（32+96*3+4*2=336→实际328）
GRID_ORIGIN_X = 144 * SCALE         # 容器在画布中的 x 坐标
GRID_ORIGIN_Y = -76 * SCALE         # 容器在画布中的 y 坐标


# 文字位置（设计稿坐标 × SCALE）
TEXT_ZH_POS = (24 * SCALE, 56 * SCALE)     # 中文标题位置
TEXT_EN_POS = (24 * SCALE, (56 + 46) * SCALE)  # 英文副标题位置 (y = 56+46=102)

# 字号
FONT_SIZE_ZH = 32 * SCALE   # 192px
FONT_SIZE_EN = 12 * SCALE   # 72px


# ============================================================
# 颜色工具
# ============================================================

def get_dominant_hue(image_path: str) -> float:
    """
    从图片中提取整体色相倾向（0.0 ~ 1.0）

    遍历所有像素的 HSL 色相，以饱和度为权重加权平均，
    得到图片的整体颜色倾向。
    """
    try:
        img = Image.open(image_path).convert("RGB")
        img = img.resize((100, 100), Image.LANCZOS)
        pixels = list(img.getdata())

        # 使用向量平均法计算平均色相（避免 0°/360° 边界问题）
        import math as _math
        sin_sum = 0.0
        cos_sum = 0.0
        weight_sum = 0.0

        for r, g, b in pixels:
            h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
            # 用饱和度作为权重，灰色像素（低饱和度）影响小
            w = s
            if w < 0.05:
                continue
            angle = h * 2 * _math.pi
            sin_sum += _math.sin(angle) * w
            cos_sum += _math.cos(angle) * w
            weight_sum += w

        if weight_sum > 0:
            avg_angle = _math.atan2(sin_sum / weight_sum, cos_sum / weight_sum)
            avg_hue = avg_angle / (2 * _math.pi)
            if avg_hue < 0:
                avg_hue += 1.0
            return avg_hue

        return 0.0  # 全灰图片回退到红色色相

    except Exception:
        return 0.0


def hue_to_background_rgb(hue: float) -> Tuple[int, int, int]:
    """
    将色相转换为背景色 hsl(hue, 32%, 32%)
    colorsys.hls_to_rgb 参数顺序: H, L, S (注意 L 和 S 的位置)
    """
    r, g, b = colorsys.hls_to_rgb(hue, 0.32, 0.32)
    return (int(r * 255), int(g * 255), int(b * 255))


# ============================================================
# 背景生成
# ============================================================

def create_background(width: int, height: int, base_color: Tuple[int, int, int]) -> Image.Image:
    """
    创建两层叠加的背景：
      - 底层：hsl(色相, 32%, 32%) 纯色
      - 上层：从上到下的黑色透明渐变（0% → 20% 不透明度）
    """
    # 底层纯色
    bg = Image.new("RGBA", (width, height), (*base_color, 255))

    # 上层黑色渐变遮罩: alpha 从 0 到 51 (20% of 255 ≈ 51)
    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    max_alpha = 51  # 20% 不透明度
    for y in range(height):
        alpha = int(max_alpha * (y / height))
        draw.line([(0, y), (width, y)], fill=(0, 0, 0, alpha))

    return Image.alpha_composite(bg, overlay)





# ============================================================
# 海报网格
# ============================================================

def add_rounded_corners(img: Image.Image, radius: int) -> Image.Image:
    """给图片添加圆角"""
    mask = Image.new("L", img.size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle([(0, 0), img.size], radius=radius, fill=255)

    result = Image.new("RGBA", img.size, (0, 0, 0, 0))
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    result.paste(img, (0, 0), mask)
    return result


def create_poster_column(
    poster_paths: List[str],
    poster_width: int,
    poster_height: int,
    gap: int,
    corner_radius: int,
) -> Image.Image:
    """
    创建一列海报（垂直排列，每列最多 3 张）
    """
    col_height = len(poster_paths) * poster_height + (len(poster_paths) - 1) * gap
    column = Image.new("RGBA", (poster_width, col_height), (0, 0, 0, 0))

    for i, path in enumerate(poster_paths):
        try:
            poster = Image.open(path)
            poster = ImageOps.fit(poster, (poster_width, poster_height), method=Image.LANCZOS)
            poster = poster.convert("RGBA")
            poster = add_rounded_corners(poster, corner_radius)

            y = i * (poster_height + gap)
            column.paste(poster, (0, y), poster)
        except Exception as e:
            logger.warning(f"处理海报 {path} 时出错: {e}")
            continue

    return column


def _place_rotated_grid(canvas: Image.Image, poster_paths: List[str]):
    """
    将旋转的海报网格直接放置到画布上

    .pen 模板中的变换逻辑：
    - 容器左上角在画布 (223.57, -95) 处
    - 容器内各列子元素有各自的 (x, y) 偏移
    - 整个容器以其左上角为原点旋转 -15°
    - 每个子元素的画布坐标 = 旋转矩阵 × 容器内坐标 + 容器原点
    """
    # 容器原点（设计稿坐标 × SCALE）
    origin_x = GRID_ORIGIN_X
    origin_y = GRID_ORIGIN_Y

    # 旋转参数
    angle_rad = math.radians(ROTATION_ANGLE)  # -15° → radians
    cos_a = math.cos(angle_rad)
    sin_a = math.sin(angle_rad)

    # 将 9 张海报分成 3 组
    grouped = [poster_paths[i:i + GRID_ROWS] for i in range(0, len(poster_paths), GRID_ROWS)]

    for col_idx, col_posters in enumerate(grouped):
        if col_idx >= GRID_COLS:
            break

        # 创建该列的海报图片
        column_img = create_poster_column(
            col_posters, POSTER_WIDTH, POSTER_HEIGHT, POSTER_GAP, POSTER_CORNER_RADIUS
        )

        # 该列在容器内的本地坐标
        local_x, local_y = COL_POSITIONS[col_idx]

        # 以容器左上角为原点，对本地坐标应用旋转
        # 旋转公式：x' = x*cos - y*sin, y' = x*sin + y*cos
        rotated_x = local_x * cos_a - local_y * sin_a + origin_x
        rotated_y = local_x * sin_a + local_y * cos_a + origin_y

        # 旋转该列图片
        rotated_col = column_img.rotate(ROTATION_ANGLE, Image.BICUBIC, expand=True)

        # 计算 expand 导致的偏移补偿
        # rotate(expand=True) 会扩大画布，新画布的中心 = 旋转后内容的中心
        # 需要补偿因扩大而产生的偏移
        cw, ch = column_img.size
        rw, rh = rotated_col.size
        # 原始中心在旋转后的位置不变，但画布尺寸变了
        # 偏移 = (新尺寸 - 旧尺寸) / 2
        offset_x = (rw - cw) / 2
        offset_y = (rh - ch) / 2

        paste_x = int(rotated_x - offset_x)
        paste_y = int(rotated_y - offset_y)

        canvas.paste(rotated_col, (paste_x, paste_y), rotated_col)


# ============================================================
# 文字绘制
# ============================================================

def _load_font(font_path: str, size: int, label: str) -> ImageFont.FreeTypeFont:
    """
    加载字体文件，带详细日志和健壮的 fallback
    """
    # 1. 尝试指定路径
    if font_path and os.path.isfile(font_path):
        try:
            font = ImageFont.truetype(font_path, size)
            logger.debug(f"字体加载成功: {label} -> {font_path}")
            return font
        except Exception as e:
            logger.warning(f"字体文件存在但加载失败: {label} -> {font_path}, 错误: {e}")
    else:
        logger.warning(f"字体文件不存在: {label} -> {font_path}")

    # 2. Fallback: Pillow 10.1+ 支持 load_default(size=N)
    try:
        font = ImageFont.load_default(size=size)
        logger.info(f"使用 Pillow 内置字体 (size={size}): {label}")
        return font
    except TypeError:
        pass

    # 3. 最终 fallback
    logger.warning(f"无法加载合适字体，使用默认小字体: {label}")
    return ImageFont.load_default()


def draw_title(
    image: Image.Image,
    title_zh: str,
    title_en: str,
    zh_font_path: str,
    en_font_path: str,
) -> Image.Image:
    """
    在画布左侧绘制中英文标题
    """
    img = image.copy()
    text_layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(text_layer)

    # 中文标题
    zh_font = _load_font(zh_font_path, FONT_SIZE_ZH, "中文字体")
    draw.text(
        TEXT_ZH_POS,
        title_zh,
        font=zh_font,
        fill=(255, 255, 255, 255),
    )

    # 英文副标题
    if title_en:
        en_font = _load_font(en_font_path, FONT_SIZE_EN, "英文字体")
        draw.text(
            TEXT_EN_POS,
            title_en,
            font=en_font,
            fill=(255, 255, 255, 204),  # #ffffffcc
        )

    return Image.alpha_composite(img, text_layer)


# ============================================================
# 主函数
# ============================================================

def create_cover(
    library_dir: str,
    title: Tuple[str, str],
    font_path: Tuple[str, str],
) -> Optional[str]:
    """
    生成媒体库封面图片

    参数:
        library_dir: 海报图片目录（内含 1.jpg ~ 9.jpg）
        title: (中文标题, 英文标题)
        font_path: (中文字体路径, 英文字体路径)

    返回:
        base64 编码的 PNG 图片字符串，失败返回 None
    """
    try:
        title_zh, title_en = title
        zh_font_path, en_font_path = font_path

        poster_folder = Path(library_dir)

        # 自定义九宫格排列顺序：把最重要的海报放在最显眼的位置
        # 315426987 表示: 位置(1,1)=3.jpg, (1,2)=1.jpg, (1,3)=5.jpg, ...
        custom_order = "315426987"
        order_map = {num: idx for idx, num in enumerate(custom_order)}

        supported_formats = (".jpg", ".jpeg", ".png", ".bmp", ".webp")

        poster_files = sorted(
            [
                os.path.join(poster_folder, f)
                for f in os.listdir(poster_folder)
                if os.path.isfile(os.path.join(poster_folder, f))
                and f.lower().endswith(supported_formats)
                and os.path.splitext(f)[0] in order_map
            ],
            key=lambda x: order_map[os.path.splitext(os.path.basename(x))[0]],
        )

        if not poster_files:
            logger.error(f"目录 {poster_folder} 中没有找到海报图片")
            return None

        # 最多取 9 张
        poster_files = poster_files[: GRID_ROWS * GRID_COLS]

        # 1. 提取色相，生成背景色 hsl(h, 32%, 32%)
        first_image = poster_files[0]
        hue = get_dominant_hue(first_image)
        bg_color = hue_to_background_rgb(hue)

        # 2. 创建背景（纯色 + 黑色渐变遮罩）
        canvas = create_background(CANVAS_WIDTH, CANVAS_HEIGHT, bg_color)

        # 3. 创建旋转海报网格并放置到画布
        # .pen 中旋转以元素左上角 (x=223.57, y=-95) 为原点
        # 使用仿射变换实现，直接将各列海报旋转后粘贴到画布
        _place_rotated_grid(canvas, poster_files)

        # 5. 绘制中英文标题
        canvas = draw_title(canvas, title_zh, title_en, zh_font_path, en_font_path)

        # 6. 导出为 base64
        buffer = io.BytesIO()
        try:
            canvas.save(buffer, format="WEBP", quality=85, optimize=True)
        except Exception:
            canvas = canvas.convert("RGB")
            canvas.save(buffer, format="JPEG", quality=85, optimize=True)

        return base64.b64encode(buffer.getvalue()).decode("utf-8")

    except Exception as e:
        logger.error(f"创建封面时出错: {e}")
        return None
