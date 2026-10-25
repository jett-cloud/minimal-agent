import pytest

from minimal_agent.tools import CALCULATOR_TOOL, ToolRegistry


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("2 + 3 * 4", "14"),
        ("(2 + 3) * 4", "20"),
        ("0.1 + 0.2", "0.3"),
        ("0.10000000000000001 + 0.2", "0.30000000000000001"),
        ("10 % 3", "1"),
        ("2 ** 10", "1024"),
        ("abs(-12.5)", "12.5"),
        ("round(1 / 3, 4)", "0.3333"),
    ],
)
async def test_calculator_evaluates_bounded_decimal_arithmetic(
    expression: str,
    expected: str,
) -> None:
    # 验证计算器执行有界十进制运算。
    result = await ToolRegistry([CALCULATOR_TOOL]).execute("calculator", {"expression": expression})

    assert result.ok is True
    assert result.data == {"expression": expression, "result": expected}


@pytest.mark.asyncio
async def test_calculator_supports_square_root() -> None:
    # 验证计算器支持平方根。
    result = await ToolRegistry([CALCULATOR_TOOL]).execute("calculator", {"expression": "sqrt(2)"})

    assert result.ok is True
    assert result.data["result"].startswith("1.4142135623730950488")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo unsafe')",
        "(1).__class__",
        "[x for x in range(3)]",
        "2 ** 1001",
        "1 / 0",
        "sqrt(-1)",
        "unknown(1)",
    ],
)
async def test_calculator_rejects_unsafe_or_invalid_expressions(expression: str) -> None:
    # 验证计算器拒绝不安全或无效表达式。
    result = await ToolRegistry([CALCULATOR_TOOL]).execute("calculator", {"expression": expression})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code in {"INVALID_EXPRESSION", "CALCULATION_LIMIT_EXCEEDED"}


@pytest.mark.asyncio
async def test_calculator_validates_expression_length() -> None:
    # 验证计算器校验表达式长度。
    result = await ToolRegistry([CALCULATOR_TOOL]).execute("calculator", {"expression": "1" * 513})

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "INVALID_ARGUMENTS"
