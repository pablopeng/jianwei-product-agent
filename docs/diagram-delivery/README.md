# 产品流程与评测闭环图

两张图已嵌入[飞书方案文档](https://bytedance.larkoffice.com/docx/TMEDdKO4xoQWhAxx8vjckdpXnvu)，第4节与第6节。画板可编辑，点击可放大。

| 图 | 飞书实际导图 | 可编辑源文件 | 独立视觉复核 |
|---|---|---|---|
| 主Agent与两阶段生成 | [真实导图](product-flow/feishu-v2.jpg) | [Mermaid](product-flow/diagram.mmd) | A 9.1 / B 9.2，无阻断项 |
| 规则—生产—评测闭环 | [真实导图](evaluation-loop/feishu-v2.jpg) | [Mermaid](evaluation-loop/diagram.mmd) | A 9.1 / B 9.0，无阻断项 |

源码通过飞书原生接口回读且一致。真实导图为飞书API默认方形画布，底部留白来自导出边界；未将本地渲染冒充飞书导图。闭环图下方两条虚线有轻微邻近，方向与标签可辨。二期Auto Rubrics、SFT与图文详情页重构均明确标为尚未实现。

验收过程遵循design-lark-chart：Normalize → Select → Plan → Mermaid自动布局/Render → 飞书写入和源码回读 → 真实导图 → 双独立视觉评审。完整证据见各目录plan、roundtrip和vqa文件。
