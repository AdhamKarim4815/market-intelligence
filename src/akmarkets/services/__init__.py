"""Application services: the only layer the CLI, web and API all call.

There is no web-only calculation and no CLI-only calculation. A figure shown
on a page and the same figure printed by a command come from one function
here, which in turn calls `akmarkets.domain`.
"""
