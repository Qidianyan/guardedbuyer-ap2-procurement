"""Goods-receipt check: compare delivered quantity/unit price/SKU against the order."""
def reconcile(req, receipt, price_tolerance=0.01):
    issues = []
    if receipt["sku"] != req.sku: issues.append(f"wrong SKU: got {receipt['sku']} expected {req.sku}")
    if receipt["quantity"] < req.quantity: issues.append(f"short shipment: got {receipt['quantity']} of {req.quantity}")
    if receipt["unit_price"] > req.unit_price * (1 + price_tolerance):
        issues.append(f"overcharged unit price {receipt['unit_price']} vs {req.unit_price}")
    good = min(receipt["quantity"], req.quantity) if receipt["sku"] == req.sku else 0
    shortfall = round(req.total - good * req.unit_price, 2)
    return {"ok": not issues, "issues": issues, "refund_due": shortfall}
