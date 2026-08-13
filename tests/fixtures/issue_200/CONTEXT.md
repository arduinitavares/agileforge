# String Calculator

This context defines the language for supplying integer lists to a calculator and
receiving either their sum or a negative-number rejection.

## Language

**String Calculator**:
A calculator that accepts one Number List and returns its arithmetic sum.
_Avoid_: Expression evaluator, formula engine

**Number List**:
Text containing zero or more Integer Tokens separated by Delimiters. An empty
Number List represents zero.
_Avoid_: Expression, formula

**Integer Token**:
A decimal integer written with ASCII digits and an optional leading minus sign,
without whitespace or a leading plus sign. Leading zeros do not change its
value, and negative zero represents zero.
_Avoid_: Number expression

**Delimiter**:
A comma or line-feed character separating adjacent Integer Tokens in a Number
List. Custom delimiters are outside the current Product Goal.
_Avoid_: Separator syntax

**Negative-number Rejection**:
The result when a Number List contains values below zero. It reports every
negative value in encounter order, including duplicates, using canonical decimal
values without leading zeros.
_Avoid_: Partial sum

**Malformed Input**:
Text that is neither empty nor a valid Number List. Its handling is outside the
current Product Goal.
_Avoid_: Negative-number Rejection
