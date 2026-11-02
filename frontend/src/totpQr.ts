import jsQR from 'jsqr';

const MAX_FILE_BYTES = 5 * 1024 * 1024;
const MAX_PIXELS = 16_000_000;
const MAX_EDGE = 2048;
const IMAGE_TYPES = new Set(['image/png', 'image/jpeg', 'image/webp']);

export async function readTotpQr(file: File): Promise<string> {
  if (!IMAGE_TYPES.has(file.type) || !file.size || file.size > MAX_FILE_BYTES) throw new Error('QR image rejected');
  let bitmap: ImageBitmap | undefined;
  let canvas: HTMLCanvasElement | undefined;
  let context: CanvasRenderingContext2D | null = null;
  let pixels: ImageData | undefined;
  try {
    bitmap = await createImageBitmap(file);
    const { width, height } = bitmap;
    if (!Number.isFinite(width) || !Number.isFinite(height) || width < 1 || height < 1 || width * height > MAX_PIXELS) throw new Error('QR image rejected');
    const scale = Math.min(1, MAX_EDGE / Math.max(width, height));
    canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    context = canvas.getContext('2d', { willReadFrequently: true });
    if (!context) throw new Error('QR image unavailable');
    context.drawImage(bitmap, 0, 0, canvas.width, canvas.height);
    pixels = context.getImageData(0, 0, canvas.width, canvas.height);
    const decoded = jsQR(pixels.data, pixels.width, pixels.height, { inversionAttempts: 'attemptBoth' });
    if (!decoded?.data || decoded.data.length > 4096) throw new Error('QR content rejected');
    const content = decoded.data.trim();
    if (!content.startsWith('otpauth://totp/') && !content.startsWith('otpauth-migration://offline?data=')) throw new Error('QR content rejected');
    return content;
  } finally {
    try {
      pixels?.data.fill(0);
      if (canvas) {
        context?.clearRect(0, 0, canvas.width, canvas.height);
        canvas.width = 0; canvas.height = 0;
      }
    } finally { bitmap?.close(); }
  }
}
