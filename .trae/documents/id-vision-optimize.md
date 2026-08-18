# 证件识别优化：提速 + 报错修复 + 图片放大

## 目标

解决后台证件识别 3 个问题 + 1 个 UI 需求：
1. **识别慢**：HKID/TW_ID 识别耗时 30-60s+
2. **识别报错**：大图 base64 超限 / LLM 超时 / 无 timeout 挂死
3. **图片放大**：上传的证件图片点击可放大查看

---

## 根因分析

### 慢/报错的 3 个根因

#### 根因 1：Vision 调用无 timeout（最严重）

[id_document_vision.py:672-675](file:///d:/projects/finance-ai/src/materials/id_document_vision.py#L672-L675) 直接 `OpenAI(...)` **没传 timeout**，用 SDK 默认（无超时或 600s）。

对比 [openai_client.py:26-35](file:///d:/projects/finance-ai/src/llm/openai_client.py#L26-L35) `LLMClient` 有 `timeout=20s`（`OPENAI_TIMEOUT_SECONDS` 默认 20s）。

**后果**：HKID/TW_ID prompt 复杂（要求繁体原文 + 住址逐字抄录），大图 + 复杂指令 → LLM 响应 30-60s+，无 timeout 时前端等到浏览器超时。

#### 根因 2：大图不压缩直接 base64

[id_document_vision.py:676-678](file:///d:/projects/finance-ai/src/materials/id_document_vision.py#L676-L678) 直接把原始图片 bytes 转 base64 data_url。手机拍照 3-5MB，base64 膨胀 33% → 4-7MB 字符串传给 LLM。

**后果**：① 传输慢 ② LLM 上下文超长报错 ③ 某些 API 网关限制 body 大小。

#### 根因 3：3 次串行 LLM 调用

`run_id_extract` 流程：
1. `recognize_id_document`（vision LLM）
2. `enrich_number_from_ocr`（ddddocr 本地）
3. `enrich_extracted_fields` → `translate_address_cn_to_en`（LLM）+ `ensure_passport_english_name`（LLM）

**后果**：vision 20s + 翻译 10s + 罗马化 10s = 40s+ 串行等待。

---

## 改动清单

### 改动 1：Vision 调用加 timeout + 图片压缩

**文件**：[src/materials/id_document_vision.py](file:///d:/projects/finance-ai/src/materials/id_document_vision.py)

**改动点 1**：`recognize_id_document` 函数里 `OpenAI(...)` 加 timeout 参数：

```python
client = OpenAI(
    api_key=settings.openai_api_key,
    base_url=settings.openai_api_base,
    timeout=float(getattr(settings, "openai_vision_timeout_seconds", 30.0) or 30.0),
)
```

**改动点 2**：`_guess_mime` 后加图片压缩函数 `_compress_image`：

```python
def _compress_image(data: bytes, *, max_dim: int = 1024, quality: int = 80) -> bytes:
    """压缩图片到 max_dim 内，JPEG quality=80。失败返回原图。"""
    try:
        from io import BytesIO
        from PIL import Image
        img = Image.open(BytesIO(data))
        # 已是小图不压
        if max(img.width, img.height) <= max_dim:
            return data
        img.thumbnail((max_dim, max_dim), Image.LANCZOS)
        # 转 RGB（PNG 透明背景 → 白底）
        if img.mode in ("RGBA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1] if img.mode == "RGBA" else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        out = BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        compressed = out.getvalue()
        logger.info(
            "证件图片压缩: %dKB → %dKB (%dx%d)",
            len(data) // 1024,
            len(compressed) // 1024,
            img.width,
            img.height,
        )
        return compressed if len(compressed) < len(data) else data
    except Exception:
        logger.debug("图片压缩跳过", exc_info=True)
        return data
```

**改动点 3**：`recognize_id_document` 调用处：

```python
# 原代码
b64 = base64.b64encode(image_bytes).decode("ascii")
# 改为
compressed = _compress_image(image_bytes)
mime = "image/jpeg"  # 压缩后统一 JPEG
b64 = base64.b64encode(compressed).decode("ascii")
data_url = f"data:{mime};base64,{b64}"
```

**改动点 4**：settings.py 加配置：

```python
openai_vision_timeout_seconds: float = Field(
    default=30.0, validation_alias="OPENAI_VISION_TIMEOUT_SECONDS"
)
```

### 改动 2：并行 Vision + OCR

**文件**：[src/materials/id_extract.py](file:///d:/projects/finance-ai/src/materials/id_extract.py)

**改动**：`run_id_extract` 里 vision 和 OCR 并行（OCR 不依赖 vision 结果）：

```python
import concurrent.futures

# 原代码：先 vision 再 OCR
vision = recognize_id_document(...)
try:
    otype, onum = enrich_number_from_ocr(...)
except Exception:
    pass

# 改为：并行
with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
    f_vision = pool.submit(
        recognize_id_document, image_bytes, filename=filename or "id.jpg", expected_id_type=expected
    )
    f_ocr = pool.submit(
        extract_id_number_ocr, image_bytes, hint_type=expected
    )
    vision = f_vision.result()
    try:
        otype, onum = f_ocr.result()
    except Exception:
        otype, onum = "", ""
# 后续合并逻辑不变
```

### 改动 3：翻译合并到 Vision prompt（减少 1 次 LLM）

**文件**：[src/materials/id_document_vision.py](file:///d:/projects/finance-ai/src/materials/id_document_vision.py)

**改动**：vision prompt 里直接要求输出 `address_en`（英文住址）和 `name_en`（护照英文名），后续 `enrich_extracted_fields` 只在 vision 没给出时才补调翻译。

**prompt 追加**（在 `user_text` 末尾）：
```
- 若有 address_cn 且为中文，同时输出 address_en=英文住址（拼音/官方英文，保留门牌楼层数字）
- 若为 PASSPORT 且 name_cn 非空但 name_en 为空，输出 name_en=拼音式英文名（姓前名后大写）
```

**解析**（`_parse_vision_payload`）：
```python
address_en = str(data.get("address_en") or "").strip()
# 若 vision 给了 address_en，写入 raw 供后续提取
```

**to_admin_fields** 加 `address_en` 输出：
```python
if self.address_en:  # 新增字段
    out["director_address_en"] = self.address_en
```

**enrich_extracted_fields** 改为只在 `director_address_en` 为空时才调 `translate_address_cn_to_en`（已有逻辑，无需改）。

### 改动 4：HKID/TW_ID prompt 精简

**文件**：[src/materials/id_document_vision.py](file:///d:/projects/finance-ai/src/materials/id_document_vision.py)

**改动**：把 4 种类型的详细指令合并为通用指令 + 类型特化，减少 prompt token 数。

原 prompt ~700 字，精简到 ~400 字：
- 通用指令（side/is_handheld/confidence/JSON 格式）
- 类型特化用表格形式（每种类型 2-3 行）

### 改动 5：图片放大 Modal

**文件**：
- [web/admin/src/components/ImageModal.tsx](file:///d:/projects/finance-ai/web/admin/src/components/ImageModal.tsx)（新建）
- [web/admin/src/pages/RegisterPage.tsx](file:///d:/projects/finance-ai/web/admin/src/pages/RegisterPage.tsx)
- [web/admin/src/pages/IdExtractPage.tsx](file:///d:/projects/finance-ai/web/admin/src/pages/IdExtractPage.tsx)
- [web/admin/src/styles.css](file:///d:/projects/finance-ai/web/admin/src/styles.css)

**ImageModal.tsx**：
```tsx
type Props = {
  src: string;
  alt?: string;
  open: boolean;
  onClose: () => void;
};

export function ImageModal({ src, alt = "", open, onClose }: Props) {
  const [scale, setScale] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const [dragging, setDragging] = useState(false);
  const [dragStart, setDragStart] = useState({ x: 0, y: 0 });

  useEffect(() => {
    if (open) {
      setScale(1);
      setOffset({ x: 0, y: 0 });
      const onKey = (e: KeyboardEvent) => {
        if (e.key === "Escape") onClose();
      };
      window.addEventListener("keydown", onKey);
      return () => window.removeEventListener("keydown", onKey);
    }
  }, [open, onClose]);

  if (!open) return null;

  const onWheel = (e: React.WheelEvent) => {
    e.preventDefault();
    setScale((s) => Math.max(0.5, Math.min(5, s - e.deltaY * 0.001)));
  };

  const onMouseDown = (e: React.MouseEvent) => {
    setDragging(true);
    setDragStart({ x: e.clientX - offset.x, y: e.clientY - offset.y });
  };
  const onMouseMove = (e: React.MouseEvent) => {
    if (!dragging) return;
    setOffset({ x: e.clientX - dragStart.x, y: e.clientY - dragStart.y });
  };
  const onMouseUp = () => setDragging(false);

  return (
    <div className="img-modal-overlay" onClick={onClose}>
      <div className="img-modal-toolbar" onClick={(e) => e.stopPropagation()}>
        <button onClick={() => setScale((s) => Math.min(5, s + 0.2))}>+</button>
        <span>{(scale * 100).toFixed(0)}%</span>
        <button onClick={() => setScale((s) => Math.max(0.5, s - 0.2))}>-</button>
        <button onClick={() => { setScale(1); setOffset({ x: 0, y: 0 }); }}>重置</button>
        <button onClick={onClose}>✕</button>
      </div>
      <div
        className="img-modal-body"
        onClick={(e) => e.stopPropagation()}
        onWheel={onWheel}
        onMouseDown={onMouseDown}
        onMouseMove={onMouseMove}
        onMouseUp={onMouseUp}
        onMouseLeave={onMouseUp}
      >
        <img
          src={src}
          alt={alt}
          style={{
            transform: `translate(${offset.x}px, ${offset.y}px) scale(${scale})`,
            cursor: dragging ? "grabbing" : "grab",
          }}
          draggable={false}
        />
      </div>
    </div>
  );
}
```

**RegisterPage.tsx 改动**：
- 证件上传后显示缩略图（现在没显示）
- 缩略图点击 → `ImageModal` 放大

```tsx
// 新增 state
const [modalSrc, setModalSrc] = useState("");

// 缩略图
{idFile && idFile.type.startsWith("image/") ? (
  <div className="id-thumb-wrap">
    <img
      src={URL.createObjectURL(idFile)}
      alt="证件缩略图"
      className="id-thumb"
      onClick={() => setModalSrc(URL.createObjectURL(idFile))}
    />
  </div>
) : null}

// Modal
<ImageModal
  src={modalSrc}
  alt="证件图片"
  open={!!modalSrc}
  onClose={() => setModalSrc("")}
/>
```

**IdExtractPage.tsx 改动**：
- `previewUrl` 图片点击 → `ImageModal` 放大

**styles.css 追加**：
```css
/* 图片放大 Modal */
.img-modal-overlay {
  position: fixed; inset: 0; z-index: 9999;
  background: rgba(0,0,0,0.85);
  display: flex; flex-direction: column; align-items: center; justify-content: center;
}
.img-modal-toolbar {
  position: absolute; top: 16px; right: 16px;
  display: flex; gap: 8px; align-items: center;
  background: rgba(255,255,255,0.1); border-radius: 6px; padding: 6px 10px;
}
.img-modal-toolbar button {
  background: none; border: none; color: #fff; font-size: 16px; cursor: pointer; padding: 2px 8px;
}
.img-modal-toolbar button:hover { background: rgba(255,255,255,0.2); border-radius: 4px; }
.img-modal-toolbar span { color: #fff; font-size: 13px; min-width: 40px; text-align: center; }
.img-modal-body {
  max-width: 90vw; max-height: 85vh; overflow: hidden;
  display: flex; align-items: center; justify-content: center;
}
.img-modal-body img {
  max-width: 100%; max-height: 100%; object-fit: contain;
  transition: transform 0.1s ease-out; user-select: none;
}

/* 缩略图 */
.id-thumb-wrap { margin-top: 8px; }
.id-thumb {
  max-width: 200px; max-height: 140px; border: 1px solid var(--border, #e8e8e8);
  border-radius: 4px; cursor: zoom-in; object-fit: contain;
}
.id-thumb:hover { opacity: 0.85; }
```

---

## 改动文件清单

| 文件 | 操作 | 改动 |
|---|---|---|
| [src/materials/id_document_vision.py](file:///d:/projects/finance-ai/src/materials/id_document_vision.py) | 修改 | OpenAI 加 timeout + `_compress_image` 图片压缩 + prompt 精简 + address_en/name_en 直接输出 |
| [src/materials/id_extract.py](file:///d:/projects/finance-ai/src/materials/id_extract.py) | 修改 | vision + OCR 并行（ThreadPoolExecutor） |
| [src/materials/id_document_translate.py](file:///d:/projects/finance-ai/src/materials/id_document_translate.py) | 不改 | 已有「仅填空」逻辑，vision 给了 address_en 就不调翻译 |
| [config/settings.py](file:///d:/projects/finance-ai/config/settings.py) | 修改 | 加 `openai_vision_timeout_seconds` 配置 |
| [web/admin/src/components/ImageModal.tsx](file:///d:/projects/finance-ai/web/admin/src/components/ImageModal.tsx) | 新建 | 图片放大 modal（缩放/拖动/ESC关闭/工具栏） |
| [web/admin/src/pages/RegisterPage.tsx](file:///d:/projects/finance-ai/web/admin/src/pages/RegisterPage.tsx) | 修改 | 证件上传后显示缩略图 + 点击放大 |
| [web/admin/src/pages/IdExtractPage.tsx](file:///d:/projects/finance-ai/web/admin/src/pages/IdExtractPage.tsx) | 修改 | 预览图点击放大 |
| [web/admin/src/styles.css](file:///d:/projects/finance-ai/web/admin/src/styles.css) | 修改 | ImageModal + 缩略图样式 |

---

## 预期效果

| 指标 | 优化前 | 优化后 |
|---|---|---|
| HKID 识别耗时 | 30-60s+（无 timeout） | 5-15s（压缩图 + 精简 prompt + timeout 30s） |
| TW_ID 识别耗时 | 30-60s+ | 5-15s |
| 大图报错 | base64 4-7MB → 上下文超限 | 压缩到 ~200KB |
| LLM 调用次数 | 3 次串行 | 1 次（vision 直出 address_en/name_en）+ 1 次 OCR 并行 |
| 图片放大 | 无 | modal 缩放/拖动/ESC 关闭 |

---

## 验证步骤

### 1. 后端 py_compile
```powershell
.venv\Scripts\python.exe -m py_compile src\materials\id_document_vision.py src\materials\id_extract.py config\settings.py
```

### 2. 图片压缩验证
```python
from src.materials.id_document_vision import _compress_image
big = open("test_big.jpg", "rb").read()  # 3MB
small = _compress_image(big)
print(f"{len(big)//1024}KB → {len(small)//1024}KB")
# 应输出 3072KB → ~150KB
```

### 3. 前端构建
```powershell
cd web/admin; npm run build
```

### 4. 端到端测试
- 管理后台 → 证件识别 → 上传 HKID 图片 → 应在 5-15s 内返回结果
- 上传大图（>2MB）→ 不报错，压缩后识别
- 点击证件缩略图 → modal 放大，滚轮缩放，拖动平移，ESC 关闭
