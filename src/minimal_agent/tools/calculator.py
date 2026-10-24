"""A bounded AST calculator that never evaluates arbitrary Python code."""

import ast
from decimal import Decimal, DecimalException, localcontext

from pydantic import BaseModel, ConfigDict, field_validator

from minimal_agent.tools.base import ToolDefinition, ToolExecutionError

_MAX_EXPRESSION_LENGTH = 512
_MAX_AST_NODES = 128
_MAX_DEPTH = 32
_MAX_POWER = 1000
_MAX_RESULT_DIGITS = 1200
_DECIMAL_PRECISION = 50


class CalculatorArguments(BaseModel):
    """Arguments for deterministic decimal arithmetic."""

    model_config = ConfigDict(extra="forbid")

    expression: str

    @field_validator("expression")
    @classmethod
    def validate_expression(cls, value: str) -> str:
        expression = value.strip()
        if not expression:
            raise ValueError("Expression must not be empty")
        if len(expression) > _MAX_EXPRESSION_LENGTH:
            raise ValueError(f"Expression must not exceed {_MAX_EXPRESSION_LENGTH} characters")
        return expression


class _SafeEvaluator:
    def __init__(self, expression: str) -> None:
        self.expression = expression

    def evaluate(self) -> Decimal:
        try:
            parsed = ast.parse(self.expression, mode="eval")
        except SyntaxError as error:
            raise ToolExecutionError(
                "INVALID_EXPRESSION",
                "Expression has invalid syntax",
                details={"offset": error.offset or 0},
            ) from error

        if sum(1 for _ in ast.walk(parsed)) > _MAX_AST_NODES:
            raise ToolExecutionError(
                "CALCULATION_LIMIT_EXCEEDED",
                "Expression contains too many operations",
            )

        try:
            with localcontext() as context:
                context.prec = _DECIMAL_PRECISION
                result = self._evaluate_node(parsed.body, depth=0)
                return self._ensure_safe(result)
        except ToolExecutionError:
            raise
        except (DecimalException, ZeroDivisionError) as error:
            raise ToolExecutionError(
                "INVALID_EXPRESSION",
                "Expression cannot be evaluated",
                details={"reason": type(error).__name__},
            ) from error

    def _evaluate_node(self, node: ast.AST, *, depth: int) -> Decimal:
        if depth > _MAX_DEPTH:
            raise ToolExecutionError(
                "CALCULATION_LIMIT_EXCEEDED",
                "Expression nesting is too deep",
            )

        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            source = ast.get_source_segment(self.expression, node)
            if source is None:
                source = str(node.value)
            try:
                return self._ensure_safe(Decimal(source.replace("_", "")))
            except DecimalException as error:
                raise ToolExecutionError(
                    "INVALID_EXPRESSION",
                    "Expression contains an invalid number",
                ) from error

        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            operand = self._evaluate_node(node.operand, depth=depth + 1)
            return self._ensure_safe(operand if isinstance(node.op, ast.UAdd) else -operand)

        if isinstance(node, ast.BinOp):
            left = self._evaluate_node(node.left, depth=depth + 1)
            right = self._evaluate_node(node.right, depth=depth + 1)
            return self._evaluate_binary(node.op, left, right)

        if isinstance(node, ast.Call):
            return self._evaluate_call(node, depth=depth + 1)

        raise ToolExecutionError(
            "INVALID_EXPRESSION",
            f"Unsupported expression element: {type(node).__name__}",
        )

    def _evaluate_binary(self, operator: ast.operator, left: Decimal, right: Decimal) -> Decimal:
        if isinstance(operator, ast.Add):
            result = left + right
        elif isinstance(operator, ast.Sub):
            result = left - right
        elif isinstance(operator, ast.Mult):
            result = left * right
        elif isinstance(operator, ast.Div):
            result = left / right
        elif isinstance(operator, ast.FloorDiv):
            result = left // right
        elif isinstance(operator, ast.Mod):
            result = left % right
        elif isinstance(operator, ast.Pow):
            if right != right.to_integral_value() or abs(right) > _MAX_POWER:
                raise ToolExecutionError(
                    "CALCULATION_LIMIT_EXCEEDED",
                    f"Power must be an integer between -{_MAX_POWER} and {_MAX_POWER}",
                )
            result = left ** int(right)
        else:
            raise ToolExecutionError(
                "INVALID_EXPRESSION",
                f"Unsupported operator: {type(operator).__name__}",
            )
        return self._ensure_safe(result)

    def _evaluate_call(self, node: ast.Call, *, depth: int) -> Decimal:
        if not isinstance(node.func, ast.Name) or node.keywords:
            raise ToolExecutionError("INVALID_EXPRESSION", "Unsupported function call")
        arguments = [self._evaluate_node(argument, depth=depth) for argument in node.args]

        if node.func.id == "abs" and len(arguments) == 1:
            return self._ensure_safe(abs(arguments[0]))
        if node.func.id == "sqrt" and len(arguments) == 1:
            if arguments[0] < 0:
                raise ToolExecutionError(
                    "INVALID_EXPRESSION",
                    "sqrt requires a non-negative argument",
                )
            return self._ensure_safe(arguments[0].sqrt())
        if node.func.id == "round" and len(arguments) in (1, 2):
            digits = 0
            if len(arguments) == 2:
                if arguments[1] != arguments[1].to_integral_value() or abs(arguments[1]) > 100:
                    raise ToolExecutionError(
                        "CALCULATION_LIMIT_EXCEEDED",
                        "round digits must be an integer between -100 and 100",
                    )
                digits = int(arguments[1])
            return self._ensure_safe(round(arguments[0], digits))

        raise ToolExecutionError(
            "INVALID_EXPRESSION",
            f"Unsupported function or argument count: {node.func.id}",
        )

    @staticmethod
    def _ensure_safe(value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ToolExecutionError("CALCULATION_LIMIT_EXCEEDED", "Result must be finite")
        digits = len(value.as_tuple().digits)
        if digits > _MAX_RESULT_DIGITS or abs(value.adjusted()) > _MAX_RESULT_DIGITS:
            raise ToolExecutionError(
                "CALCULATION_LIMIT_EXCEEDED",
                "Intermediate result is too large",
            )
        return value


def _format_decimal(value: Decimal) -> str:
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def calculate(arguments: CalculatorArguments) -> dict[str, str]:
    """Evaluate a bounded arithmetic expression and return its exact decimal text."""

    result = _SafeEvaluator(arguments.expression).evaluate()
    return {"expression": arguments.expression, "result": _format_decimal(result)}


CALCULATOR_TOOL = ToolDefinition(
    name="calculator",
    description=(
        "Evaluate a precise arithmetic expression. Supports +, -, *, /, //, %, integer powers, "
        "parentheses, abs, sqrt, and round. Use it when arithmetic accuracy is important."
    ),
    args_model=CalculatorArguments,
    handler=calculate,
)
