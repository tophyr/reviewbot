"""Shopping cart helpers.

A cart is a list of lines. Each line is a dict with "price" (a number, in
cents) and "quantity" (a non-negative integer).
"""


def line_total(item):
    """Return price * quantity for one cart line."""
    return item["price"] * item["quantity"]
