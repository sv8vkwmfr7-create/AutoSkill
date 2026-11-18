# 执行器对比基准（local vs docker）

## 后端：local（7/7 通过）

| id | 判定 | 路径 | 技能 | 执行耗时s | 端到端s | 错误 |
|----|------|------|------|-----------|---------|------|
| N01 | ✅ | generated | text_sha256_hash | 0.073 | 34.4 |  |
| N02 | ✅ | generated | reverse_string | 0.076 | 30.53 |  |
| N03 | ✅ | generated | compute_gcd | 0.067 | 44.72 |  |
| N04 | ✅ | generated | camel_to_snake | 0.061 | 36.14 |  |
| N05 | ✅ | generated | luhn_validate_card_number | 0.067 | 51.24 |  |
| N06 | ✅ | generated | base64_encode_text | 0.07 | 57.76 |  |
| SEC01 | ✅ | security | - | 0 | 0 |  |

## 指标说明

- `exec_s`：沙箱内技能执行耗时（子进程/容器生命周期）
- `total_s`：含 LLM 生成/裁决的端到端耗时
- `SEC01`：别名攻击越权读取，任何后端都必须显式拒绝
