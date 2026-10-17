"""Create a reviewable paragraph / figure example using authored test translations.

No paid API is used. This demonstrates grouping and layout, not model quality.
"""
from pathlib import Path
import json
import sys
import threading

import httpx
import pymupdf as fitz

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from papertranslator.api import ChatClient
from papertranslator.extraction import analyze
from papertranslator.layout import export_pdf
from papertranslator.models import Options, Provider
from papertranslator.translation import PLACEHOLDER, translate_project


def make_demo(root):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    source = root / "段落与整图示例_原文.pdf"
    output = root / "段落与整图示例_译文.pdf"
    with fitz.open() as doc:
        p = doc.new_page(width=600, height=780)
        p.insert_text((45, 38), "Paragraph and figure translation", fontsize=19, fontname="hebo")
        p.insert_textbox((45, 70, 550, 145), "Scientific documents should be translated one complete paragraph at a time. A paragraph keeps its meaning even when the source PDF stores each word in a different font or on a separate line.", fontsize=12)
        p.insert_textbox((45, 170, 550, 245), "All labels in the same figure share a common context. Their translations should use consistent terms while the curves, numerical values, and label directions remain in their original positions.", fontsize=12)
        p.insert_text((45, 270), "For the proof, let", fontsize=12)
        p.insert_text((140, 270), "a", fontname="symb", fontsize=12)
        p.insert_text((154, 270), "be the coefficient.", fontsize=12)
        p.draw_rect((72, 315, 540, 650), color=(0.7, 0.75, 0.8), width=0.5)
        p.draw_line((115, 600), (510, 600), width=0.8)
        p.draw_line((115, 600), (115, 370), width=0.8)
        p.draw_polyline([(115, 570), (190, 510), (270, 440), (350, 405), (430, 390), (505, 382)], color=(0.15, 0.35, 0.85), width=2)
        p.draw_polyline([(115, 580), (190, 535), (270, 475), (350, 435), (430, 420), (505, 415)], color=(0.9, 0.3, 0.2), width=2)
        p.insert_text((145, 345), "Training accuracy", fontsize=12, color=(0.15, 0.35, 0.85))
        p.insert_text((335, 345), "Validation accuracy", fontsize=12, color=(0.9, 0.3, 0.2))
        p.insert_text((85, 520), "Accuracy", fontsize=12, rotate=90)
        p.insert_text((285, 628), "Epochs", fontsize=12)
        for x, text in [(110, "0"), (265, "20"), (500, "50")]:
            p.insert_text((x, 616), text, fontsize=9)
        for y, text in [(595, "0.0"), (490, "0.5"), (380, "1.0")]:
            p.insert_text((94, y), text, fontsize=9)
        p.insert_textbox((45, 677, 550, 715), "Fig. 1 | Training and validation accuracy over 50 epochs.", fontsize=11)
        p.insert_text((45, 752), "Offline layout example; authored translations, not a live API result.", fontsize=9)
        doc.save(source)
    options = Options(ocr=False, translate_figures=False, vision_review=False)
    project_file = root / "demo.paragraphs.ptrans.json"
    # This cache belongs only to the freshly recreated synthetic demo above.
    if project_file.exists():
        project_file.unlink()
    project = analyze(str(source), options, str(project_file), threading.Event(), lambda _: None)
    def response(request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][-1]["content"])
        result = []
        for item in data["segments"]:
            text = item["source"]
            values = PLACEHOLDER.findall(text)
            if "Scientific documents" in text:
                translated = "科学文献应当以完整段落为单位翻译。即使原 PDF 使用不同字体存储各个单词，或将文字分散在多行中，也应将整段内容连贯理解并翻译。"
            elif "All labels" in text:
                translated = "同一张图中的所有标签共享完整的图示上下文。翻译时应统一术语，同时保持曲线、数值以及标签的位置和方向，让整张图的原文与译文相互对应。"
            elif "Paragraph and figure" in text:
                translated = "段落与整图翻译"
            elif "For the proof" in text:
                translated = f"在证明中，令 {values[0]} 为系数。"
            elif "Fig." in text:
                translated = f"图 {values[0]}｜训练准确率与验证准确率随 {values[1]} 个训练轮次的变化。"
            elif "Training accuracy" in text:
                translated = "训练准确率"
            elif "Validation accuracy" in text:
                translated = "验证准确率"
            elif text.strip() == "Accuracy":
                translated = "准确率"
            elif text.strip() == "Epochs":
                translated = "训练轮次"
            elif "Offline layout" in text:
                translated = "离线排版示例：使用预先编写的测试译文，并非真实接口翻译结果。"
            else:
                translated = text
            result.append({"id": item["id"], "translation": translated, "preserve": False})
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"segments": result}, ensure_ascii=False)}, "finish_reason": "stop"}]})
    with_client = ChatClient(Provider(api_key="offline-demo-key"), transport=httpx.MockTransport(response))
    try:
        translate_project(project, options, with_client, lambda: project.save(project_file), lambda _: None)
    finally:
        with_client.close()
    report = export_pdf(project, output, options)
    project.save(project_file)
    with fitz.open(output) as doc:
        doc[0].get_pixmap(dpi=110).save(root / "段落与整图示例.png")
    print(json.dumps({"pdf": str(output), "units": [{"kind": u.kind, "members": len(u.block_ids)} for u in project.units], "needs_review": report["needs_review"]}, ensure_ascii=False))
    return output


if __name__ == "__main__":
    make_demo(sys.argv[1] if len(sys.argv) > 1 else "test-output/v0.2.0-demo")
