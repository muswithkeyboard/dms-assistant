"""Сборка маленьких docx для тестов чтения: свой document.xml / numbering.xml / footnotes.xml / картинки.
Без документов организаторов (`docs/rules.md` п. 4): только синтетический текст."""
from __future__ import annotations

import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

NSDECL = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
          'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
          'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
          'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
          'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
          'xmlns:v="urn:schemas-microsoft-com:vml" '
          'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
          'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"')

# PNG 1×1 (две разные картинки — разные хэши)
PNG_A = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                      "1f15c4890000000d49444154789c6360f80f0000010101005a4d6e4a0000000049454e44ae426082")
PNG_B = PNG_A[:-20] + b"\x00" * 4 + PNG_A[-16:]


def p(text: str = "", num: tuple[int, int] | None = None, style: str | None = None, hidden_mark: bool = False,
      runs: str | None = None) -> str:
    """Абзац: num=(numId, ilvl); runs — готовые w:r (иначе один прогон с text)."""
    ppr = ""
    if style:
        ppr += f'<w:pStyle w:val="{style}"/>'
    if num:
        ppr += f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>'
    if hidden_mark:
        ppr += "<w:rPr><w:vanish/></w:rPr>"
    body = runs if runs is not None else (r(text) if text else "")
    return f"<w:p>{'<w:pPr>' + ppr + '</w:pPr>' if ppr else ''}{body}</w:p>"


def r(text: str, hidden: bool = False) -> str:
    rpr = "<w:rPr><w:vanish/></w:rPr>" if hidden else ""
    return f'<w:r>{rpr}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def fnref(fid: int) -> str:
    return f'<w:r><w:footnoteReference w:id="{fid}"/></w:r>'


def blip(rid: str) -> str:
    """DrawingML-картинка внутри mc:AlternateContent с VML-дублем в Fallback (как сохраняет Word)."""
    drawing = (f'<w:drawing><wp:inline><a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="{rid}"/>'
               "</pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing>")
    fallback = f'<w:pict><v:shape><v:imagedata r:id="{rid}"/></v:shape></w:pict>'
    return (f"<w:r><mc:AlternateContent><mc:Choice Requires=\"wps\">{drawing}</mc:Choice>"
            f"<mc:Fallback>{fallback}</mc:Fallback></mc:AlternateContent></w:r>")


def vml(rid: str) -> str:
    """VML-картинка (так вставлены иконки у Ренессанса)."""
    return f'<w:r><w:pict><v:shape><v:imagedata r:id="{rid}"/></v:shape></w:pict></w:r>'


def tbl(*rows: list[str]) -> str:
    """Таблица: каждая строка — список содержимого ячеек (готовые абзацы)."""
    out = ["<w:tbl>"]
    for row in rows:
        out.append("<w:tr>" + "".join(f"<w:tc>{c or '<w:p/>'}</w:tc>" for c in row) + "</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def lvl(ilvl: int, text: str, fmt: str = "decimal", start: int = 1, is_lgl: bool = False,
        extra: str = "") -> str:
    lgl = "<w:isLgl/>" if is_lgl else ""
    return (f'<w:lvl w:ilvl="{ilvl}"><w:start w:val="{start}"/><w:numFmt w:val="{fmt}"/>{lgl}'
            f'<w:lvlText w:val="{text}"/>{extra}</w:lvl>')


def abstract(aid: int, *levels: str) -> str:
    return f'<w:abstractNum w:abstractNumId="{aid}">{"".join(levels)}</w:abstractNum>'


def num(nid: int, aid: int, overrides: str = "") -> str:
    return f'<w:num w:numId="{nid}"><w:abstractNumId w:val="{aid}"/>{overrides}</w:num>'


def build(body: str, numbering: str = "", styles: str = "", footnotes: dict[int, str] | None = None,
          images: dict[str, bytes] | None = None, directory: Path | None = None, name: str = "t.docx") -> Path:
    """Собрать docx и вернуть путь (во временной папке, если directory не задан)."""
    d = directory or Path(tempfile.mkdtemp(prefix="dms_docx_"))
    path = d / name
    rels = [('<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
            'relationships/styles" Target="styles.xml"/>')]
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml",
                   '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                   '<Default Extension="xml" ContentType="application/xml"/>'
                   '<Default Extension="png" ContentType="image/png"/></Types>')
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {NSDECL}><w:body>{body}</w:body>'
                                        "</w:document>")
        if numbering:
            z.writestr("word/numbering.xml", f'<?xml version="1.0"?><w:numbering {NSDECL}>{numbering}'
                                             "</w:numbering>")
        z.writestr("word/styles.xml", f'<?xml version="1.0"?><w:styles {NSDECL}>{styles}</w:styles>')
        if footnotes:
            notes = "".join(f'<w:footnote w:id="{i}"><w:p><w:r><w:footnoteRef/></w:r>{r(t)}</w:p></w:footnote>'
                            for i, t in footnotes.items())
            z.writestr("word/footnotes.xml", f'<?xml version="1.0"?><w:footnotes {NSDECL}>'
                                             '<w:footnote w:id="0"><w:p/></w:footnote>'
                                             f"{notes}</w:footnotes>")
        for rid, data in (images or {}).items():
            z.writestr(f"word/media/{rid}.png", data)
            rels.append(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                        f'relationships/image" Target="media/{rid}.png"/>')
        z.writestr("word/_rels/document.xml.rels",
                   '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                   f'relationships">{"".join(rels)}</Relationships>')
    return path
