"""
test_ax.py — Quick smoke test for the deep accessibility tree.
==============================================================
This is the FIRST thing to run to confirm Phase 1's keystone works. It does NOT
move the mouse or touch anything — it only reads the accessibility tree of the
frontmost app and prints the numbered element list.

Usage:
    python test_ax.py            # 2-second delay, then read frontmost app
    python test_ax.py 4          # 4-second delay (time to switch apps)

What to check:
    1. Open a NATIVE app (e.g. System Settings, Notes) → you should see many
       numbered CLICK/TYPE elements with sensible labels.
    2. Open Chrome/Safari on a real page (e.g. google.com) → after the browser
       accessibility kicks in, you should see page links/fields, not just the
       toolbar. If the list is near-empty on web pages, that's the signal to
       lean on Phase 2 (DOM injection) for the browser path.
"""

import sys
import time
import ax_tree


def main() -> None:
    delay = 2.0
    if len(sys.argv) > 1:
        try:
            delay = float(sys.argv[1])
        except ValueError:
            pass

    print(f"\nSwitch to the app you want to inspect. Reading in {delay:.0f}s…")
    time.sleep(delay)

    pid, name, bundle = ax_tree.get_frontmost_app()
    print(f"\nFrontmost app: {name}")
    print(f"  pid={pid}  bundle={bundle}")

    t0 = time.monotonic()
    elements = ax_tree.get_elements()
    dt_ms = (time.monotonic() - t0) * 1000

    print(f"\nFound {len(elements)} interactive elements in {dt_ms:.0f} ms\n")
    print(ax_tree.format_for_prompt(elements))

    if elements:
        print("\nReal coordinates (these come from the OS — the model never sees them):")
        for e in elements[:12]:
            print(f"  [{e['id']:>2}] ({e['x']:>4},{e['y']:>4})  {e['w']:>3}x{e['h']:<3}  "
                  f"{e['role']:<16} :: {e['name']}")
    else:
        print("\n⚠️  No elements found. Checklist:")
        print("    • Did you grant Accessibility permission to this terminal?")
        print("      System Settings → Privacy & Security → Accessibility")
        print("    • For browsers, give the page a second to expose its tree, then retry.")


if __name__ == "__main__":
    main()
