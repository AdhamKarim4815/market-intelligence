"""External data adapters behind one interface per data category.

No provider is load-bearing. Each category declares a Protocol, concrete
adapters implement it, and a fallback chain tries them in configured order.
Every response carries its source, effective time and retrieval time, so a
value on screen can always say where it came from and how old it is.
"""
