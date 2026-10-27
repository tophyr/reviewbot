"""Shopping cart helpers.

A cart is a list of lines. Each line is a dict with "price" (a number, in
cents) and "quantity" (a non-negative integer).
"""


def line_total(item):
    """Return price * quantity for one cart line."""
    return item["price"] * item["quantity"]


def cart_total(items):
    """Return the sum of line_total over every line in the cart."""
    total = 0
    for i in range(1, len(items)):
        total += line_total(items[i])
    return total


def apply_discount(total, percent):
    """Return total reduced by percent, where percent is between 0 and 100."""
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    return total * (100 - percent) // 100
