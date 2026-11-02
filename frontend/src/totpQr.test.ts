import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import jsQR from 'jsqr';
import { readTotpQr } from './totpQr';

vi.mock('jsqr', () => ({ default: vi.fn() }));

const syntheticUri = 'otpauth://totp/Instagram:synthetic?secret=JBSWY3DPEHPK3PXP&issuer=Instagram';
let close: ReturnType<typeof vi.fn>;
let draw: ReturnType<typeof vi.fn>;
let clear: ReturnType<typeof vi.fn>;
let getPixels: ReturnType<typeof vi.fn>;
let buffer: Uint8ClampedArray;

function result(data: string) { return { data } as NonNullable<ReturnType<typeof jsQR>>; }
function image(type = 'image/png', bytes = 16) { return new File([new Uint8Array(bytes)], 'synthetic.png', { type }); }
beforeEach(() => {
  buffer = new Uint8ClampedArray(16).fill(127);
  close = vi.fn(); draw = vi.fn(); clear = vi.fn();
  getPixels = vi.fn((_x: number, _y: number, width: number, height: number) => ({ data: buffer, width, height, colorSpace: 'srgb' } as ImageData));
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ drawImage: draw, clearRect: clear, getImageData: getPixels } as unknown as CanvasRenderingContext2D);
  vi.stubGlobal('createImageBitmap', vi.fn(async () => ({ width: 4000, height: 3000, close })));
  vi.mocked(jsQR).mockReset().mockReturnValue(result(syntheticUri));
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

describe('Lokaler Authenticator-QR-Import', () => {
  it('begrenzt den Canvas auf 2048 Pixel und gibt Bitmap sowie Bilddaten nach dem Lesen frei', async () => {
    await expect(readTotpQr(image())).resolves.toBe(syntheticUri);
    expect(getPixels).toHaveBeenCalledWith(0, 0, 2048, 1536);
    const canvas = vi.mocked(HTMLCanvasElement.prototype.getContext).mock.contexts[0] as HTMLCanvasElement;
    expect(canvas.width).toBe(0); expect(canvas.height).toBe(0);
    expect(clear).toHaveBeenCalledWith(0, 0, 2048, 1536);
    expect(close).toHaveBeenCalledOnce();
    expect(buffer.every(value => value === 0)).toBe(true);
  });

  it.each(['image/gif', 'text/plain', ''])('lehnt den Dateityp %s vor dem Decodieren ab', async type => {
    await expect(readTotpQr(image(type))).rejects.toThrow();
    expect(createImageBitmap).not.toHaveBeenCalled();
    expect(HTMLCanvasElement.prototype.getContext).not.toHaveBeenCalled();
  });

  it.each([0, 5 * 1024 * 1024 + 1])('lehnt die Dateigröße %i vor dem Decodieren ab', async bytes => {
    await expect(readTotpQr(image('image/png', bytes))).rejects.toThrow();
    expect(createImageBitmap).not.toHaveBeenCalled();
  });

  it('prüft mehr als 16 Megapixel vor dem Canvas und schließt die Bitmap auch bei Ablehnung', async () => {
    vi.mocked(createImageBitmap).mockResolvedValueOnce({ width: 4001, height: 4000, close } as unknown as ImageBitmap);
    await expect(readTotpQr(image())).rejects.toThrow();
    expect(HTMLCanvasElement.prototype.getContext).not.toHaveBeenCalled();
    expect(close).toHaveBeenCalledOnce();
    expect(jsQR).not.toHaveBeenCalled();
  });

  it.each(['otpauth://totp/Instagram:synthetic?secret=JBSWY3DPEHPK3PXP', 'otpauth-migration://offline?data=synthetic'])('akzeptiert ausschließlich unterstützte Einrichtungsinhalte: %s', async content => {
    vi.mocked(jsQR).mockReturnValueOnce(result(content));
    await expect(readTotpQr(image('image/webp'))).resolves.toBe(content);
  });

  it.each(['https://example.test', 'otpauth://hotp/Instagram:synthetic', '123456', 'otpauth://totp/' + 'A'.repeat(4096)])('verwirft ungeeignete oder zu lange QR-Inhalte und bereinigt Ressourcen', async content => {
    vi.mocked(jsQR).mockReturnValueOnce(result(content));
    await expect(readTotpQr(image('image/jpeg'))).rejects.toThrow();
    expect(close).toHaveBeenCalledOnce(); expect(clear).toHaveBeenCalledOnce();
    expect(buffer.every(value => value === 0)).toBe(true);
  });

  it('bereinigt die Bilddaten auch wenn kein QR-Code erkannt wird', async () => {
    vi.mocked(jsQR).mockReturnValueOnce(null);
    await expect(readTotpQr(image())).rejects.toThrow();
    expect(close).toHaveBeenCalledOnce(); expect(clear).toHaveBeenCalledOnce();
    expect(buffer.every(value => value === 0)).toBe(true);
  });

  it('schließt die Bitmap auch wenn der Canvas nicht verfügbar ist', async () => {
    vi.mocked(HTMLCanvasElement.prototype.getContext).mockReturnValueOnce(null);
    await expect(readTotpQr(image())).rejects.toThrow();
    expect(close).toHaveBeenCalledOnce();
    expect(jsQR).not.toHaveBeenCalled();
  });
});
