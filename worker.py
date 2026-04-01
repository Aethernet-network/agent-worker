#!/usr/bin/env python3
"""AetherNet autonomous worker agent.

Claims tasks, completes them with Claude, submits results with evidence.
All operations are cryptographically signed with AETHERNET-TX-V1.

Usage:
    pip install aethernet-sdk anthropic
    export ANTHROPIC_API_KEY=sk-ant-...
    python worker.py
"""

import os
import sys
import time
import json
import traceback

import anthropic

from aethernet.signing import get_or_create_keypair
from aethernet.client import AetherNetClient


# ── Configuration ────────────────────────────────────────────────────────────

NODE_URL = os.environ.get("AETHERNET_NODE", "https://testnet.aethernet.network")
KEY_NAME = os.environ.get("AGENT_KEY_NAME", "agent-worker")
CATEGORIES = os.environ.get("CATEGORIES", "research,analysis,code").split(",")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "15"))
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-20250514")


# ── Setup ────────────────────────────────────────────────────────────────────

def setup():
    """Create signing keypair, connect to testnet, register agent."""
    signing_key = get_or_create_keypair(KEY_NAME)
    client = AetherNetClient(NODE_URL, signing_key=signing_key)

    print(f"Agent ID:   {client.agent_id[:16]}...")
    print(f"Node:       {NODE_URL}")
    print(f"Categories: {', '.join(CATEGORIES)}")
    print(f"Model:      {MODEL}")
    print()

    # Register (idempotent — safe on every startup)
    try:
        result = client.register()
        grant = result.get("onboarding_allocation", 0)
        if grant:
            print(f"Registered with onboarding grant: {grant:,} µAET")
        else:
            print("Registered (no grant — already onboarded)")
    except Exception as e:
        if "409" in str(e) or "429" in str(e):
            print("Already registered")
        else:
            print(f"Registration: {e}")

    claude = anthropic.Anthropic()
    return client, claude


# ── Work Execution ───────────────────────────────────────────────────────────

def do_work(claude, task):
    """Use Claude to complete a task. Returns (output, evidence)."""
    prompt = f"""Complete this task thoroughly and accurately.

Title: {task['title']}
Description: {task['description']}
Category: {task.get('category', 'general')}

Requirements:
- Provide a complete, well-structured response
- Include specific details, not vague generalities
- If research is requested, cite specific sources
- If code is requested, include working, tested code
- If analysis is requested, show your reasoning step by step

Respond with your complete work product."""

    start = time.time()
    response = claude.messages.create(
        model=MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    elapsed_ms = int((time.time() - start) * 1000)

    output = response.content[0].text
    tokens = response.usage.output_tokens

    evidence = {
        "model": MODEL,
        "methodology": f"Direct LLM completion with structured prompt ({task.get('category', 'general')} category)",
        "output_tokens": tokens,
        "execution_time_ms": elapsed_ms,
        "input_tokens": response.usage.input_tokens,
    }

    return output, evidence


# ── Main Loop ────────────────────────────────────────────────────────────────

def run(client, claude):
    """Poll for tasks, claim, work, submit. Runs forever."""
    claimed_count = 0
    completed_count = 0

    while True:
        try:
            # Poll for open tasks in our categories
            for category in CATEGORIES:
                category = category.strip()
                if not category:
                    continue

                try:
                    tasks = client.list_tasks(status="open", category=category)
                except Exception:
                    tasks = []

                if not tasks:
                    continue

                for task in tasks:
                    task_id = task["id"]
                    budget = task.get("budget", 0)
                    title = task.get("title", "Untitled")

                    # Claim the task
                    try:
                        client.claim_task(task_id)
                        claimed_count += 1
                        print(f"[CLAIMED] {title[:60]} (budget: {budget:,} µAET)")
                    except Exception as e:
                        # Already claimed by someone else, or other error
                        continue

                    # Do the work
                    try:
                        output, evidence = do_work(claude, task)
                        print(f"[WORKED]  {len(output)} chars, {evidence['output_tokens']} tokens, {evidence['execution_time_ms']}ms")
                    except Exception as e:
                        print(f"[ERROR]   Work failed: {e}")
                        continue

                    # Submit the result
                    try:
                        client.submit_task_result(
                            task_id,
                            result_hash=f"sha256:{hash(output) & 0xFFFFFFFFFFFFFFFF:016x}",
                            result_note=output[:200],
                            result_content=output,
                            evidence=evidence,
                        )
                        completed_count += 1
                        print(f"[SUBMIT]  {title[:60]}")
                        print(f"          claimed={claimed_count} completed={completed_count}")
                    except Exception as e:
                        print(f"[ERROR]   Submit failed: {e}")

        except KeyboardInterrupt:
            print(f"\nShutting down. Claimed: {claimed_count}, Completed: {completed_count}")
            sys.exit(0)
        except Exception as e:
            print(f"[ERROR] Loop: {e}")
            traceback.print_exc()

        time.sleep(POLL_INTERVAL)


# ── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("AetherNet Agent Worker")
    print("=" * 40)
    print()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not set")
        print("  export ANTHROPIC_API_KEY=sk-ant-...")
        sys.exit(1)

    client, claude = setup()
    print()
    print(f"Polling every {POLL_INTERVAL}s for tasks in: {', '.join(CATEGORIES)}")
    print("Press Ctrl+C to stop")
    print()

    run(client, claude)
