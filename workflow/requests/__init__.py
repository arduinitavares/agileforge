"""Guarded request contracts for workflow graph transitions."""

from workflow.requests.project_shell import AbandonProjectShell, OpenProjectShell

type TransitionRequest = OpenProjectShell | AbandonProjectShell

__all__ = ["AbandonProjectShell", "OpenProjectShell", "TransitionRequest"]
