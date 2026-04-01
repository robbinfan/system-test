"""
Vespa Evaluator - Actor-based test evaluation framework for Vespa system tests.

Architecture:
    Plan (LLM) → Case Selector → Actor Executor (K8s Pods) → Report

Inspired by Erlang/OTP actor model:
    - Each test case runs as an isolated Actor in a K8s Pod
    - Supervisor manages actor lifecycle and failure recovery
    - Message passing between actors via asyncio queues
"""

__version__ = "0.1.0"
