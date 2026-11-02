/// <reference types="node" />
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, expect, it, vi } from 'vitest';
import jsQR from 'jsqr';
import { readTotpQr } from './totpQr';

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('liest eine echte synthetische QR-Grafik mit dem gebündelten jsQR-Decoder', async () => {
  const directory = join(dirname(fileURLToPath(import.meta.url)), 'test-fixtures');
  const fixture = JSON.parse(readFileSync(join(directory, 'synthetic-instagram-qr.json'), 'utf8')) as { width: number; height: number; value: string };
  // Die RGBA-Pixel gehören zur mitgelieferten PNG. Nur das native Browser-Decoding wird simuliert.
  const pixels = new Uint8ClampedArray(readFileSync(join(directory, 'synthetic-instagram-qr.rgba')));
  const image = new File([new Uint8Array(readFileSync(join(directory, 'synthetic-instagram-qr.png')))], 'synthetic.png', { type: 'image/png' });
  const close = vi.fn(); const clear = vi.fn();
  vi.stubGlobal('createImageBitmap', vi.fn(async () => ({ width: fixture.width, height: fixture.height, close })));
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    drawImage: vi.fn(), clearRect: clear,
    getImageData: () => ({ data: pixels, width: fixture.width, height: fixture.height, colorSpace: 'srgb' }),
  } as unknown as CanvasRenderingContext2D);
  expect(vi.isMockFunction(jsQR)).toBe(false);
  await expect(readTotpQr(image)).resolves.toBe(fixture.value);
  expect(close).toHaveBeenCalledOnce();
  expect(clear).toHaveBeenCalledWith(0, 0, fixture.width, fixture.height);
  expect(pixels.every(value => value === 0)).toBe(true);
});
