import io
import re
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader


@dataclass(frozen=True)
class Chunk:
    text: str
    page: int | None
    ordinal: int


def split_text(text: str, size: int = 700, overlap: int = 100) -> list[str]:
    if size < 2 or not 0 <= overlap < size:
        raise ValueError('切分参数需要满足 0 <= overlap < size。')
    text = re.sub(r'\n{3,}', '\n\n', text.replace('\x00', '')).strip()
    result = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            boundary = max(text.rfind(c, start + size // 2, end) for c in '\n。！？；')
            if boundary >= 0:
                end = boundary + 1
        piece = text[start:end].strip()
        if piece:
            result.append(piece)
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return result


def parse_document(name: str, content: bytes) -> list[Chunk]:
    suffix = Path(name).suffix.lower()
    pages: list[tuple[int | None, str]] = []
    if suffix in {'.txt', '.md'}:
        encoding = 'utf-16' if content.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
        try:
            text = content.decode(encoding)
        except UnicodeDecodeError:
            try:
                text = content.decode('gb18030')
            except UnicodeDecodeError as exc:
                raise ValueError('文本编码无法识别，请转成 UTF-8 后重试。') from exc
        pages.append((None, text))
    elif suffix == '.pdf':
        try:
            reader = PdfReader(io.BytesIO(content))
            if reader.is_encrypted:
                raise ValueError('暂不支持加密 PDF，请先解密。')
            if len(reader.pages) > 300:
                raise ValueError('PDF 最多支持 300 页，请拆分后导入。')
            pages = [(i + 1, page.extract_text() or '') for i, page in enumerate(reader.pages)]
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError('PDF 解析失败，请检查文件是否损坏。') from exc
    else:
        raise ValueError('仅支持 .txt、.md、.pdf 文件。')
    chunks = []
    for page, text in pages:
        for part in split_text(text):
            chunks.append(Chunk(part, page, len(chunks) + 1))
            if len(chunks) > 3000:
                raise ValueError('文档分块过多，请拆分后导入。')
    if not chunks:
        raise ValueError('未提取到文字。扫描 PDF 需要先经过 OCR，本项目暂不提供 OCR。')
    return chunks
