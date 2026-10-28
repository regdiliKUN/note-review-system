"""生成测试用 docx：文字与图片混排，含错误事实与零散条目。"""
import os
from docx import Document
from docx.shared import Inches, Pt, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from PIL import Image, ImageDraw

OUT = os.path.dirname(os.path.abspath(__file__))


def make_img(path, text, color):
    im = Image.new("RGB", (720, 380), color)
    d = ImageDraw.Draw(im)
    for i in range(0, 720, 40):
        d.line([(i, 0), (i, 380)], fill=(255, 255, 255, 40), width=1)
    d.rectangle([30, 30, 690, 350], outline=(255, 255, 255), width=3)
    d.text((60, 170), text, fill=(255, 255, 255))
    im.save(path)


make_img(os.path.join(OUT, "fig1.png"), "FIG 1  -  pipeline overview", (38, 78, 148))
make_img(os.path.join(OUT, "fig2.png"), "FIG 2  -  data flow", (18, 110, 90))
make_img(os.path.join(OUT, "fig3.png"), "FIG 3  -  result chart", (120, 50, 110))

doc = Document()
st = doc.styles["Normal"]
st.font.name = "微软雅黑"
st.font.size = Pt(11)

doc.add_heading("第三章 深度学习训练流程", level=1)

doc.add_paragraph("深度学习的训练过程主要分为前向传播和反向传播两个阶段。前向传播是把输入数据送入网络,"
                  "逐层计算得到输出;反向传播则是根据损失函数计算梯度,再用梯度下降法更新参数。")

doc.add_paragraph("图1展示了整体的训练流水线：")
doc.add_picture(os.path.join(OUT, "fig1.png"), width=Inches(5.2))
doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER

doc.add_paragraph("其中学习率是最重要的超参数,一般设置在 0.001 到 0.01 之间都能训练。"
                  "如果学习率太大,损失会震荡不收敛;如果太小,收敛会很慢。")

doc.add_heading("3.1 优化器", level=2)
doc.add_paragraph("常用的优化器有 SGD、Momentum、Adam 等。Adam 结合了动量法和自适应学习率,"
                  "在绝大多数任务上都能取得不错的效果,是 2014 年由 Kingma 等人提出的。")
doc.add_picture(os.path.join(OUT, "fig2.png"), width=Inches(4.6))
doc.add_paragraph("上图是数据在各层之间的流动示意。")

doc.add_heading("3.2 正则化", level=2)
doc.add_paragraph("为了防止过拟合,常用的手段有 L1/L2 正则化、Dropout、数据增强。"
                  "Dropout 在训练时随机丢弃一部分神经元,相当于训练了多个子网络再集成。")

doc.add_paragraph("补充几点笔记:")
doc.add_paragraph("· BatchNorm 可以加速收敛")
doc.add_paragraph("· 早停 early stopping 也是常用手段")
doc.add_paragraph("· 学习率预热 warmup 对大模型很重要")

doc.add_heading("3.3 实验结果", level=2)
doc.add_paragraph("我们在 ImageNet 上做了实验,ResNet50 的 top-1 准确率达到了 76.1%,"
                  "比 VGG16 高了大概 5 个百分点。训练用了 8 张 V100 显卡,总共训练了 90 个 epoch。")
doc.add_picture(os.path.join(OUT, "fig3.png"), width=Inches(4.4))

doc.add_paragraph("遗留问题:混合精度训练具体怎么实现还没搞明白,需要后面补。")

# 一个表格，验证表格内图片与文字
t = doc.add_table(rows=2, cols=2)
t.style = "Table Grid"
t.cell(0, 0).text = "模型"
t.cell(0, 1).text = "准确率"
t.cell(1, 0).text = "ResNet50"
t.cell(1, 1).text = "76.1%"

p = os.path.join(OUT, "示例笔记.docx")
doc.save(p)
print("saved", p)
