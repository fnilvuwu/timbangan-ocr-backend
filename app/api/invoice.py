from datetime import datetime
from io import BytesIO

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

from app.schemas.invoice import InvoiceGenerateRequest

router = APIRouter(prefix="/invoice", tags=["invoice"])


def _to_money(value: float) -> str:
    return f"Rp {value:,.0f}".replace(",", ".")


@router.post("/pdf")
def generate_invoice_pdf(payload: InvoiceGenerateRequest) -> StreamingResponse:
    issued_at = payload.issued_at or datetime.now()
    total_price = payload.total_price
    if total_price is None:
        total_price = round(payload.weight * payload.price_per_kg)

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    page_width, page_height = A4

    margin_x = 50
    y = page_height - 60

    pdf.setTitle(f"Invoice {payload.invoice_no}")
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(margin_x, y, "INVOICE")
    y -= 24
    pdf.setFont("Helvetica", 11)
    pdf.drawString(margin_x, y, f"Invoice No: {payload.invoice_no}")
    y -= 18
    pdf.drawString(margin_x, y, f"Date: {issued_at.strftime('%d %b %Y %H:%M')}")
    y -= 26

    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(margin_x, y, "Store")
    y -= 16
    pdf.setFont("Helvetica", 11)
    pdf.drawString(margin_x, y, payload.store_name)
    y -= 24

    if payload.customer_name:
        pdf.drawString(margin_x, y, f"Customer: {payload.customer_name}")
        y -= 18
    if payload.cashier_name:
        pdf.drawString(margin_x, y, f"Cashier: {payload.cashier_name}")
        y -= 18

    y -= 10
    pdf.line(margin_x, y, page_width - margin_x, y)
    y -= 20

    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(margin_x, y, "Description")
    pdf.drawString(300, y, "Qty")
    pdf.drawString(380, y, "Unit Price")
    pdf.drawRightString(page_width - margin_x, y, "Subtotal")
    y -= 14
    pdf.line(margin_x, y, page_width - margin_x, y)
    y -= 20

    pdf.setFont("Helvetica", 11)
    pdf.drawString(margin_x, y, "Commodity Weighing")
    pdf.drawString(300, y, f"{payload.weight:.2f} kg")
    pdf.drawString(380, y, _to_money(payload.price_per_kg))
    pdf.drawRightString(page_width - margin_x, y, _to_money(total_price))
    y -= 24
    pdf.line(margin_x, y, page_width - margin_x, y)
    y -= 24

    pdf.setFont("Helvetica-Bold", 14)
    pdf.drawRightString(page_width - margin_x, y, f"Total: {_to_money(total_price)}")
    y -= 30

    if payload.notes:
        pdf.setFont("Helvetica", 10)
        pdf.drawString(margin_x, y, f"Notes: {payload.notes}")

    pdf.showPage()
    pdf.save()

    buffer.seek(0)
    safe_invoice_no = "".join(
        ch for ch in payload.invoice_no if ch.isalnum() or ch in "-_"
    )
    filename = f"{safe_invoice_no or 'invoice'}.pdf"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return StreamingResponse(buffer, media_type="application/pdf", headers=headers)
