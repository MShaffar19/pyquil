##############################################################################
# Copyright 2016-2017 Rigetti Computing
#
#    Licensed under the Apache License, Version 2.0 (the "License");
#    you may not use this file except in compliance with the License.
#    You may obtain a copy of the License at
#
#        http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS,
#    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#    See the License for the specific language governing permissions and
#    limitations under the License.
##############################################################################

import operator
from numbers import Number
from typing import Any, List, Iterator, Callable, Union

import numpy as np
from antlr4 import InputStream, CommonTokenStream, ParseTreeWalker
from antlr4.IntervalSet import IntervalSet
from antlr4.Token import CommonToken
from antlr4.error.ErrorListener import ErrorListener
from antlr4.error.Errors import InputMismatchException
from numpy.ma import sin, cos, sqrt, exp

from pyquil.gates import QUANTUM_GATES
from pyquil.quilatom import (
    Addr,
    MemoryReference,
    Parameter,
    WaveformReference,
    Waveform,
    Frame,
    FormalArgument,
    quil_cos,
    quil_cis,
    quil_exp,
    quil_sin,
    quil_sqrt,
    _contained_mrefs,
)
from pyquil.quiltwaveforms import (
    _waveform_classes,
    _wf_from_dict,
)
from pyquil.quilbase import (
    Gate,
    DefGate,
    DefPermutationGate,
    DefGateByPaulis,
    Measurement,
    JumpTarget,
    Label,
    Expression,
    Nop,
    Halt,
    Jump,
    JumpWhen,
    JumpUnless,
    Reset,
    Wait,
    ClassicalNot,
    ClassicalNeg,
    ClassicalAnd,
    ClassicalInclusiveOr,
    ClassicalExclusiveOr,
    ClassicalMove,
    ClassicalConvert,
    ClassicalExchange,
    ClassicalLoad,
    ClassicalStore,
    ClassicalEqual,
    ClassicalGreaterEqual,
    ClassicalGreaterThan,
    ClassicalLessEqual,
    ClassicalLessThan,
    ClassicalAdd,
    ClassicalSub,
    ClassicalMul,
    ClassicalDiv,
    RawInstr,
    Qubit,
    Pragma,
    Declare,
    AbstractInstruction,
    ClassicalTrue,
    ClassicalFalse,
    ClassicalOr,
    ResetQubit,
    Pulse,
    SetFrequency,
    ShiftFrequency,
    SetPhase,
    ShiftPhase,
    SwapPhase,
    SetScale,
    Capture,
    RawCapture,
    DefCalibration,
    DefMeasureCalibration,
    DefWaveform,
    DefFrame,
    DelayFrames,
    DelayQubits,
    Fence,
    FenceAll,
)
from .gen3.QuilLexer import QuilLexer
from .gen3.QuilListener import QuilListener
from .gen3.QuilParser import QuilParser


def run_parser(quil):
    # type: (str) -> List[AbstractInstruction]
    """
    Run the ANTLR parser.

    :param str quil: a single or multiline Quil program
    :return: list of instructions that were parsed
    """
    # Step 1: Run the Lexer
    input_stream = InputStream(quil)
    lexer = QuilLexer(input_stream)
    stream = CommonTokenStream(lexer)

    # Step 2: Run the Parser
    parser = QuilParser(stream)
    parser.removeErrorListeners()
    parser.addErrorListener(CustomErrorListener())
    tree = parser.quil()

    # Step 3: Run the Listener
    pyquil_listener = PyQuilListener()
    walker = ParseTreeWalker()
    walker.walk(pyquil_listener, tree)

    return pyquil_listener.result


class CustomErrorListener(ErrorListener):
    def syntaxError(self, recognizer, offendingSymbol, line, column, msg, e):
        # type: (QuilParser, CommonToken, int, int, str, InputMismatchException) -> None
        expected_tokens = self.get_expected_tokens(recognizer, e.getExpectedTokens()) if e else []

        raise RuntimeError(
            "Error encountered while parsing the quil program at line {} and column {}\n".format(
                line, column + 1
            )
            + "Received an '{}' but was expecting one of [ {} ]".format(
                offendingSymbol.text, ", ".join(expected_tokens)
            )
        )

    def get_expected_tokens(self, parser, interval_set):
        # type: (QuilParser, IntervalSet) -> Iterator
        """
        Like the default getExpectedTokens method except that it will fallback to the rule name if
        the token isn't a literal. For instance, instead of <INVALID> for  integer it will return
        the rule name: INT
        """
        for tok in interval_set:
            literal_name = parser.literalNames[tok]
            symbolic_name = parser.symbolicNames[tok]

            if literal_name != "<INVALID>":
                yield literal_name
            else:
                yield symbolic_name


class PyQuilListener(QuilListener):
    """
    Functions are invoked when the parser reaches the various different constructs in Quil.
    """

    def __init__(self):
        self.result = []  # type: List[AbstractInstruction]
        self.previous_result = None  # type: List[AbstractInstruction]

    def exitDefGate(self, ctx: QuilParser.DefGateContext):
        gate_name = ctx.name().getText()
        gate_type = ctx.gatetype()
        if gate_type and gate_type.getText() == "PERMUTATION":
            permutation = _permutation(ctx.matrix())
            self.result.append(DefPermutationGate(gate_name, permutation))
        else:
            matrix = _matrix(ctx.matrix())
            parameters = [_variable(v) for v in ctx.variable()]
            self.result.append(DefGate(gate_name, matrix, parameters))

    def exitDefGateAsPauli(self, ctx: QuilParser.DefGateAsPauliContext):
        from pyquil.paulis import PauliSum

        gate_name = ctx.name().getText()
        parameters = [_variable(c) for c in ctx.variable()]
        arguments = [_formalQubit(q) for q in ctx.qubitVariable()]
        body = PauliSum([_pauliTerm(t) for t in ctx.pauliTerms().pauliTerm()])
        self.result.append(DefGateByPaulis(gate_name, parameters, arguments, body))

    # DEFCIRCUIT parsing:
    # When we enter a circuit definition we create a backup of the instructions seen up to that
    # point. Then, when the listener continues walking through the circuit instructions it will
    # add to an empty list. Once we leave the circuit we then take all those instructions, shove
    # them into a RawInstr (since PyQuil has no support for circuit definitions yet), recover the
    # backup, and then continue on our way.

    def enterDefCircuit(self, ctx: QuilParser.DefCircuitContext) -> None:
        self.previous_result = self.result
        self.result = []

    def exitDefCircuit(self, ctx: QuilParser.DefCircuitContext):
        circuit_name = ctx.name().getText()
        variables = [variable.getText() for variable in ctx.variable()]
        qubitVariables = [qubitVariable.getText() for qubitVariable in ctx.qubitVariable()]
        space = " " if qubitVariables else ""

        if variables:
            raw_defcircuit = "DEFCIRCUIT {}({}){}{}:".format(
                circuit_name, ", ".join(variables), space, " ".join(qubitVariables)
            )
        else:
            raw_defcircuit = "DEFCIRCUIT {}{}{}:".format(
                circuit_name, space, " ".join(qubitVariables)
            )

        raw_defcircuit += "\n    ".join([""] + [instr.out() for instr in self.result])
        self.previous_result.append(RawInstr(raw_defcircuit))

        self.result = self.previous_result
        self.previous_result = None

    def exitGate(self, ctx: QuilParser.GateContext):
        gate_name = ctx.name().getText()
        modifiers = [mod.getText() for mod in ctx.modifier()]
        params = list(map(_param, ctx.param()))
        qubits = list(map(_qubit, ctx.qubit()))

        # The parsed string 'DAGGER CONTROLLED X 0 1' gives
        #   modifiers ['DAGGER', 'CONTROLLED']
        #   qubits    ['0', '1']
        #
        # We will build such gates by applying modifiers from right to left,
        # e.g. X 1 -> CONTROLLED X 0 1 -> DAGGER CONTROLLED X 0 1

        # Some gate modifiers increase the arity of the base gate.
        # The new qubit arguments prefix the old ones.
        modifier_qubits = []
        for m in modifiers:
            if m in ["CONTROLLED", "FORKED"]:
                modifier_qubits.append(qubits[len(modifier_qubits)])

        base_qubits = qubits[len(modifier_qubits) :]

        # Each FORKED doubles the number of parameters,
        # e.g. FORKED RX(0.5, 1.5) 0 1 has two.
        forked_offset = len(params) >> modifiers.count("FORKED")
        base_params = params[:forked_offset]

        if gate_name in QUANTUM_GATES:
            if base_params:
                gate = QUANTUM_GATES[gate_name](*base_params, *base_qubits)
            else:
                gate = QUANTUM_GATES[gate_name](*base_qubits)
        else:
            gate = Gate(gate_name, base_params, base_qubits)

        # Track the last param used (for FORKED)
        for modifier in modifiers[::-1]:
            if modifier == "CONTROLLED":
                gate.controlled(modifier_qubits.pop())
            elif modifier == "DAGGER":
                gate.dagger()
            elif modifier == "FORKED":
                gate.forked(modifier_qubits.pop(), params[forked_offset : (2 * forked_offset)])
                forked_offset *= 2
            else:
                raise ValueError(f"Unsupported gate modifier {modifier}.")

        self.result.append(gate)

    def exitCircuitGate(self, ctx: QuilParser.CircuitGateContext):
        """
        PyQuil has no constructs yet for representing gate instructions within a DEFCIRCUIT (i.e.
        gates where the qubits are inputs to the call to the circuit). Therefore we parse them as a
        raw instructions.
        """
        gate_name = ctx.name().getText()
        params = [param.getText() for param in ctx.param()]
        qubits = [qubit.getText() for qubit in ctx.circuitQubit()]
        if params:
            self.result.append(
                RawInstr("{}({}) {}".format(gate_name, ", ".join(params), " ".join(qubits)))
            )
        else:
            self.result.append(RawInstr("{} {}".format(gate_name, " ".join(qubits))))

    def exitCircuitMeasure(self, ctx: QuilParser.CircuitMeasureContext):
        qubit = ctx.circuitQubit().getText()
        classical = None
        if ctx.addr():
            classical = ctx.addr().getText()
        self.result.append(
            RawInstr(f"MEASURE {qubit} {classical}" if classical else f"MEASURE {qubit}")
        )

    def exitMeasure(self, ctx: QuilParser.MeasureContext):
        qubit = _qubit(ctx.qubit())
        classical = None
        if ctx.addr():
            classical = _addr(ctx.addr())
        self.result.append(Measurement(qubit, classical))

    def exitDefLabel(self, ctx):
        # type: (QuilParser.DefLabelContext) -> None
        self.result.append(JumpTarget(_label(ctx.label())))

    def exitHalt(self, ctx):
        # type: (QuilParser.HaltContext) -> None
        self.result.append(Halt())

    def exitJump(self, ctx):
        # type: (QuilParser.JumpContext) -> None
        self.result.append(Jump(_label(ctx.label())))

    def exitJumpWhen(self, ctx):
        # type: (QuilParser.JumpWhenContext) -> None
        self.result.append(JumpWhen(_label(ctx.label()), _addr(ctx.addr())))

    def exitJumpUnless(self, ctx):
        # type: (QuilParser.JumpUnlessContext) -> None
        self.result.append(JumpUnless(_label(ctx.label()), _addr(ctx.addr())))

    def exitResetState(self, ctx):
        # type: (QuilParser.ResetStateContext) -> None
        if ctx.qubit():
            self.result.append(ResetQubit(_qubit(ctx.qubit())))
        else:
            self.result.append(Reset())

    def exitCircuitResetState(self, ctx: QuilParser.ResetStateContext):
        qubit = ctx.circuitQubit().getText()
        self.result.append(RawInstr(f"RESET {qubit}"))

    def exitWait(self, ctx):
        # type: (QuilParser.WaitContext) -> None
        self.result.append(Wait())

    def exitClassicalUnary(self, ctx):
        # type: (QuilParser.ClassicalUnaryContext) -> None
        if ctx.TRUE():
            self.result.append(ClassicalTrue(_addr(ctx.addr())))
        elif ctx.FALSE():
            self.result.append(ClassicalFalse(_addr(ctx.addr())))
        elif ctx.NOT():
            self.result.append(ClassicalNot(_addr(ctx.addr())))
        elif ctx.NEG():
            self.result.append(ClassicalNeg(_addr(ctx.addr())))

    def exitLogicalBinaryOp(self, ctx):
        # type: (QuilParser.LogicalBinaryOpContext) -> None
        left = _addr(ctx.addr(0))
        right: Union[int, MemoryReference]
        if ctx.INT():
            right = int(ctx.INT().getText())
        else:
            right = _addr(ctx.addr(1))

        if ctx.AND():
            self.result.append(ClassicalAnd(left, right))
        elif ctx.OR():
            if isinstance(right, MemoryReference):
                self.result.append(ClassicalOr(left, right))
            else:
                raise RuntimeError(
                    "Right operand of deprecated OR instruction must be a"
                    f" MemoryReference, but found '{right}'"
                )
        elif ctx.IOR():
            self.result.append(ClassicalInclusiveOr(left, right))
        elif ctx.XOR():
            self.result.append(ClassicalExclusiveOr(left, right))

    def exitArithmeticBinaryOp(self, ctx):
        # type : (QuilParser.ArithmeticBinaryOpContext) -> None
        left = _addr(ctx.addr(0))
        if ctx.number():
            right = _number(ctx.number())
        else:
            right = _addr(ctx.addr(1))

        if ctx.ADD():
            self.result.append(ClassicalAdd(left, right))
        elif ctx.SUB():
            self.result.append(ClassicalSub(left, right))
        elif ctx.MUL():
            self.result.append(ClassicalMul(left, right))
        elif ctx.DIV():
            self.result.append(ClassicalDiv(left, right))

    def exitMove(self, ctx):
        # type: (QuilParser.MoveContext) -> None
        target = _addr(ctx.addr(0))
        if ctx.number():
            source = _number(ctx.number())
        else:
            source = _addr(ctx.addr(1))

        self.result.append(ClassicalMove(target, source))

    def exitExchange(self, ctx):
        # type: (QuilParser.ExchangeContext) -> None
        self.result.append(ClassicalExchange(_addr(ctx.addr(0)), _addr(ctx.addr(1))))

    def exitConvert(self, ctx):
        # type: (QuilParser.ConvertContext) -> None
        self.result.append(ClassicalConvert(_addr(ctx.addr(0)), _addr(ctx.addr(1))))

    def exitLoad(self, ctx):
        # type: (QuilParser.LoadContext) -> None
        self.result.append(ClassicalLoad(_addr(ctx.addr(0)), ctx.IDENTIFIER(), _addr(ctx.addr(1))))

    def exitStore(self, ctx):
        # type: (QuilParser.StoreContext) -> None
        if ctx.number():
            right = _number(ctx.number())
        else:
            right = _addr(ctx.addr(1))
        self.result.append(ClassicalStore(ctx.IDENTIFIER(), _addr(ctx.addr(0)), right))

    def exitNop(self, ctx):
        # type: (QuilParser.NopContext) -> None
        self.result.append(Nop())

    def exitClassicalComparison(self, ctx):
        # type: (QuilParser.ClassicalComparisonContext) -> None
        target = _addr(ctx.addr(0))
        left = _addr(ctx.addr(1))
        if ctx.number():
            right = _number(ctx.number())
        else:
            right = _addr(ctx.addr(2))

        if ctx.EQ():
            self.result.append(ClassicalEqual(target, left, right))
        elif ctx.GT():
            self.result.append(ClassicalGreaterThan(target, left, right))
        elif ctx.GE():
            self.result.append(ClassicalGreaterEqual(target, left, right))
        elif ctx.LT():
            self.result.append(ClassicalLessThan(target, left, right))
        elif ctx.LE():
            self.result.append(ClassicalLessEqual(target, left, right))

    def exitInclude(self, ctx):
        # type: (QuilParser.IncludeContext) -> None
        self.result.append(RawInstr(ctx.INCLUDE().getText() + " " + ctx.STRING().getText()))

    def exitPragma(self, ctx):
        # type: (QuilParser.PragmaContext) -> None
        args = list(map(lambda x: x.getText(), ctx.pragma_name()))
        if ctx.STRING():
            # [1:-1] is used to strip the quotes from the parsed string
            self.result.append(
                Pragma(
                    (ctx.IDENTIFIER() or ctx.keyword()).getText(),
                    args,
                    ctx.STRING().getText()[1:-1],
                )
            )
        else:
            self.result.append(Pragma((ctx.IDENTIFIER() or ctx.keyword()).getText(), args))

    def exitMemoryDescriptor(self, ctx):
        # type: (QuilParser.MemoryDescriptorContext) -> None
        name = ctx.IDENTIFIER(0).getText()
        memory_type = ctx.IDENTIFIER(1).getText()
        if ctx.INT():
            memory_size = int(ctx.INT().getText())
        else:
            memory_size = 1
        if ctx.SHARING():
            shared_region = ctx.IDENTIFIER(2).getText()
            offsets = [
                (int(offset_ctx.INT().getText()), offset_ctx.IDENTIFIER().getText())
                for offset_ctx in ctx.offsetDescriptor()
            ]
        else:
            shared_region = None
            offsets = []
        self.result.append(
            Declare(name, memory_type, memory_size, shared_region=shared_region, offsets=offsets)
        )

    def exitDefFrame(self, ctx: QuilParser.DefFrameContext):
        options = {}
        frame = _frame(ctx.frame())

        def _add_option(item):
            attr = item.frameAttr().getText()
            if attr == "DIRECTION":
                options["direction"] = _str_contents(item.STRING().getText())
            elif attr == "HARDWARE-OBJECT":
                options["hardware_object"] = _str_contents(item.STRING().getText())
            elif attr == "INITIAL-FREQUENCY":
                options["initial_frequency"] = _expression(item.expression())
            elif attr == "SAMPLE-RATE":
                options["sample_rate"] = _expression(item.expression())
            elif attr == "CENTER-FREQUENCY":
                options["center_frequency"] = _expression(item.expression())
            else:
                raise ValueError(f"Unexpected attribute {attr} in definition of frame {frame}")

        for item in ctx.frameSpec():
            _add_option(item)

        self.result.append(DefFrame(frame, **options))

    def enterDefCalibration(self, ctx: QuilParser.DefCalibrationContext):
        self.previous_result = self.result
        self.result = []

    def exitDefCalibration(self, ctx: QuilParser.DefCalibrationContext):
        name = ctx.name().getText()
        parameters = list(map(_param, ctx.param()))
        for p in parameters:
            mrefs = _contained_mrefs(p)
            if mrefs:
                raise ValueError(
                    f"Unexpected memory references {mrefs} in DEFCAL {name}. Did you forget a '%'?"
                )

        qubits = list(map(_formalQubit, ctx.formalQubit()))
        instrs = self.result

        self.result = self.previous_result
        self.previous_result = None
        self.result.append(DefCalibration(name, parameters, qubits, instrs))

    def enterDefMeasCalibration(self, ctx: QuilParser.DefMeasCalibrationContext):
        self.previous_result = self.result
        self.result = []

    def exitDefMeasCalibration(self, ctx: QuilParser.DefMeasCalibrationContext):
        memory_reference = FormalArgument(ctx.name().getText()) if ctx.name() else None
        qubit = _formalQubit(ctx.formalQubit())
        instrs = self.result

        self.result = self.previous_result
        self.previous_result = None
        self.result.append(DefMeasureCalibration(qubit, memory_reference, instrs))

    def exitDefWaveform(self, ctx: QuilParser.DefWaveformContext):
        name = _waveform_name(ctx.waveformName())
        if name in _waveform_classes:
            raise ValueError(f"Attempted to redefine built-in template waveform {name}.")
        parameters = [param.getText() for param in ctx.param()]
        entries = sum(_matrix(ctx.matrix()), [])
        self.result.append(DefWaveform(name, parameters, entries))

    def exitPulse(self, ctx: QuilParser.PulseContext):
        frame = _frame(ctx.frame())
        waveform = _waveform(ctx.waveform())
        self.result.append(Pulse(frame, waveform, nonblocking=True if ctx.NONBLOCKING() else False))

    def exitSetFrequency(self, ctx: QuilParser.SetFrequencyContext):
        frame = _frame(ctx.frame())
        freq = _expression(ctx.expression())
        self.result.append(SetFrequency(frame, freq))

    def exitShiftFrequency(self, ctx: QuilParser.ShiftFrequencyContext):
        frame = _frame(ctx.frame())
        freq = _expression(ctx.expression())
        self.result.append(ShiftFrequency(frame, freq))

    def exitSetPhase(self, ctx: QuilParser.SetPhaseContext):
        frame = _frame(ctx.frame())
        phase = _expression(ctx.expression())
        self.result.append(SetPhase(frame, phase))

    def exitShiftPhase(self, ctx: QuilParser.ShiftPhaseContext):
        frame = _frame(ctx.frame())
        phase = _expression(ctx.expression())
        self.result.append(ShiftPhase(frame, phase))

    def exitSwapPhase(self, ctx: QuilParser.SwapPhaseContext):
        frameA = _frame(ctx.frame(0))
        frameB = _frame(ctx.frame(1))
        self.result.append(SwapPhase(frameA, frameB))

    def exitSetScale(self, ctx: QuilParser.SetScaleContext):
        frame = _frame(ctx.frame())
        scale = _expression(ctx.expression())
        self.result.append(SetScale(frame, scale))

    def exitCapture(self, ctx: QuilParser.CaptureContext):
        frame = _frame(ctx.frame())
        kernel = _waveform(ctx.waveform())
        memory_region = _addr(ctx.addr())
        self.result.append(
            Capture(frame, kernel, memory_region, nonblocking=True if ctx.NONBLOCKING() else False)
        )

    def exitRawCapture(self, ctx: QuilParser.RawCaptureContext):
        frame = _frame(ctx.frame())
        duration = _expression(ctx.expression())
        memory_region = _addr(ctx.addr())
        self.result.append(
            RawCapture(
                frame, duration, memory_region, nonblocking=True if ctx.NONBLOCKING() else False
            )
        )

    def exitDelay(self, ctx: QuilParser.DelayContext):
        qubits = [_formalQubit(q) for q in ctx.formalQubit()]
        explicit_frames = [s.getText().strip('"') for s in ctx.STRING()]
        duration = _expression(ctx.expression())
        if explicit_frames:
            delay = DelayFrames([Frame(qubits, name) for name in explicit_frames], duration)
        else:
            delay = DelayQubits(qubits, duration)
        self.result.append(delay)

    def exitFence(self, ctx: QuilParser.FenceContext):
        qubits = list(map(_formalQubit, ctx.formalQubit()))
        self.result.append(Fence(qubits))

    def exitFenceAll(self, ctx: QuilParser.FenceContext):
        self.result.append(FenceAll())


"""
Helper functions for converting from ANTLR internals to PyQuil objects
"""


def _formalQubit(fq):
    try:
        return _qubit(fq)
    except ValueError:
        return FormalArgument(fq.getText())


def _pauliTerm(term):
    # type: (QuilParser.PauliTermContext) -> PauliTerm
    from pyquil.paulis import PauliTerm

    letters = term.IDENTIFIER().getText()
    args = [_formalQubit(q) for q in term.qubitVariable()]
    coeff = _expression(term.expression())
    return PauliTerm.from_list(list(zip(letters, args)), coeff)


def _qubit(qubit):
    # type: (QuilParser.QubitContext) -> Qubit
    return Qubit(int(qubit.getText()))


def _param(param):
    # type: (QuilParser.ParamContext) -> Any
    if param.expression():
        return _expression(param.expression())
    else:
        raise RuntimeError("Unexpected param: " + param.getText())


def _variable(variable):
    # type: (QuilParser.VariableContext) -> Parameter
    return Parameter(variable.IDENTIFIER().getText())


def _matrix(matrix):
    # type: (QuilParser.MatrixContext) -> List[List[Any]]
    out = []
    for row in matrix.matrixRow():
        out.append(list(map(_expression, row.expression())))
    return out


def _permutation(matrix):
    row = matrix.matrixRow()
    if len(row) == 1:
        return [_expression(e) for e in row[0].expression()]
    else:
        raise RuntimeError(
            "Permutation gates are defined by a single row, but found "
            + str(len(row))
            + " during parsing."
        )


def _addr(classical):
    # type: (QuilParser.AddrContext) -> MemoryReference
    if classical.IDENTIFIER() is not None:
        if classical.INT() is not None:
            return MemoryReference(str(classical.IDENTIFIER()), int(classical.INT().getText()))
        else:
            return MemoryReference(str(classical.IDENTIFIER()), 0)
    else:
        return Addr(int(classical.INT().getText()))


def _label(label):
    # type: (QuilParser.LabelContext) -> Label
    return Label(label.IDENTIFIER().getText())


def _expression(expression):
    # type: (QuilParser.ExpressionContext) -> Any
    """
    NB: Order of operations is already dealt with by the grammar. Here we can simply match on the
    type.
    """
    if isinstance(expression, QuilParser.ParenthesisExpContext):
        return _expression(expression.expression())
    elif isinstance(expression, QuilParser.PowerExpContext):
        if expression.POWER():
            return _binary_exp(expression, operator.pow)
    elif isinstance(expression, QuilParser.MulDivExpContext):
        if expression.TIMES():
            return _binary_exp(expression, operator.mul)
        elif expression.DIVIDE():
            return _binary_exp(expression, operator.truediv)
    elif isinstance(expression, QuilParser.AddSubExpContext):
        if expression.PLUS():
            return _binary_exp(expression, operator.add)
        elif expression.MINUS():
            return _binary_exp(expression, operator.sub)
    elif isinstance(expression, QuilParser.SignedExpContext):
        if expression.sign().PLUS():
            return _expression(expression.expression())
        elif expression.sign().MINUS():
            return -1 * _expression(expression.expression())
    elif isinstance(expression, QuilParser.FunctionExpContext):
        return _apply_function(expression.function(), _expression(expression.expression()))
    elif isinstance(expression, QuilParser.AddrExpContext):
        return _addr(expression.addr())
    elif isinstance(expression, QuilParser.NumberExpContext):
        return _number(expression.number())
    elif isinstance(expression, QuilParser.VariableExpContext):
        return _variable(expression.variable())

    raise RuntimeError("Unexpected expression type:" + expression.getText())


def _named_parameters(params):
    ret = dict()
    for param in params:
        name = param.IDENTIFIER().getText()
        expr = _expression(param.expression())
        ret[name] = expr
    return ret


def _binary_exp(expression, op):
    # type: (QuilParser.ExpressionContext, Callable) -> Number
    """
    Apply an operator to two expressions. Start by evaluating both sides of the operator.
    """
    [arg1, arg2] = expression.expression()
    return op(_expression(arg1), _expression(arg2))


def _apply_function(func, arg):
    # type: (QuilParser.FunctionContext, Any) -> Any
    if isinstance(arg, Expression):
        if func.SIN():
            return quil_sin(arg)
        elif func.COS():
            return quil_cos(arg)
        elif func.SQRT():
            return quil_sqrt(arg)
        elif func.EXP():
            return quil_exp(arg)
        elif func.CIS():
            return quil_cis(arg)
        else:
            raise RuntimeError("Unexpected function to apply: " + func.getText())
    else:
        if func.SIN():
            return sin(arg)
        elif func.COS():
            return cos(arg)
        elif func.SQRT():
            return sqrt(arg)
        elif func.EXP():
            return exp(arg)
        elif func.CIS():
            return cos(arg) + complex(0, 1) * sin(arg)
        else:
            raise RuntimeError("Unexpected function to apply: " + func.getText())


def _number(number):
    # type: (QuilParser.NumberContext) -> Any
    if number.realN():
        return _sign(number) * _real(number.realN())
    elif number.imaginaryN():
        return _sign(number) * complex(0, _real(number.imaginaryN().realN()))
    elif number.I():
        return _sign(number) * complex(0, 1)
    elif number.PI():
        return _sign(number) * np.pi
    else:
        raise RuntimeError("Unexpected number: " + number.getText())


def _real(real):
    # type: (QuilParser.RealNContext) -> Any
    if real.FLOAT():
        return float(real.getText())
    elif real.INT():
        return int(real.getText())
    else:
        raise RuntimeError("Unexpected real: " + real.getText())


def _sign(real):
    return -1 if real.MINUS() else 1


def _waveform(ctx: QuilParser.WaveformContext) -> Waveform:
    # type: (QuilParser.WaveformContext) -> Waveform
    name = _waveform_name(ctx.waveformName())
    param_dict = _named_parameters(ctx.namedParam())
    if param_dict:
        return _wf_from_dict(name, param_dict)
    else:
        return WaveformReference(name)


def _waveform_name(ctx: QuilParser.WaveformNameContext) -> str:
    return "/".join([spec.getText() for spec in ctx.name()])


def _str_contents(x):
    return x.strip('"')


def _frame(frame):
    # type (QuilParser.FrameContext) -> Frame
    qubits = [_formalQubit(q) for q in frame.formalQubit()]
    name = _str_contents(frame.STRING().getText())
    return Frame(qubits, name)
