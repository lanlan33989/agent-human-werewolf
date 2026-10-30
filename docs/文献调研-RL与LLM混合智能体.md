# 狼人杀中 RL 策略智能体与 LLM 协同的文献依据

> 调研：杜凡，2026-10-29。本文档为背景调研/汇报素材，不构成本期迭代任务。

已有研究表明，在狼人杀等不完全信息语言博弈中，将语言理解与策略决策分开优化，可以在特定实验设置下优于纯 LLM Agent。**Xu 等（ICML 2024）**提出由 LLM 推断隐藏身份并生成候选行动、再由强化学习（RL）策略选择行动的框架；在七人狼人杀实验中，与六名真人同场时，混合 Agent 的总体胜率为 **52%**，高于纯 LLM 的 **42%**；与六个混合 Agent 同场时则为 **50% 对 42%**（[论文](https://proceedings.mlr.press/v235/xu24ad.html)）。**Wu 等（2024）**提出 LLM 与外部 *Thinker* 协作，由 LLM 处理语言任务、由结合 18,800 局人类对局数据及强化学习训练的 Thinker 处理领域推理，报告其在九人狼人杀的推理、发言和对局评测中有效（[论文](https://arxiv.org/abs/2402.02330)）。**Jin 等（NeurIPS 2024）**在 *One Night Ultimate Werewolf* 中以 RL 训练讨论策略，再指导语言 Agent 选择交流战术，实验显示该框架在多种游戏设置下具有效果和泛化能力（[论文](https://proceedings.neurips.cc/paper_files/paper/2024/hash/8cea78701eb986f3ec357eb9b7c6badd-Abstract-Conference.html)）。这些结果支持在本项目中尝试“**LLM 提取信息和生成角色发言 + 独立 RL 网络负责策略决策**”的结构，但不能据此断言该结构在所有模型与对局中必然占优，仍需与现有纯 LLM Agent 做同条件对照。
