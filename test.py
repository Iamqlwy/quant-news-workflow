"""
GIL 性能测试：对比有 GIL (base) vs 无 GIL (quant-nogil) 环境

CPU 密集型任务（纯 Python，不调用会释放 GIL 的 C 扩展）：
  1. 蒙特卡洛 π — 浮点随机数 + 循环
  2. 纯 Python 矩阵乘法 — 三层循环
  3. Mandelbrot 集合 — 浮点密集迭代

运行方式：
  conda activate base        && python test.py    # 有 GIL
  conda activate quant-nogil && python test.py    # 无 GIL
"""
import random
import sys
import threading
import time


# ============================================================
# 工具
# ============================================================
def _run_threads(worker_fn, num_workers: int) -> float:
    """启动 num_workers 个线程执行 worker_fn，返回耗时(秒)"""
    threads = [threading.Thread(target=worker_fn) for _ in range(num_workers)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return time.perf_counter() - t0


# ============================================================
# 任务 1：蒙特卡洛求 π
# ============================================================
def _monte_carlo_pi(iterations: int) -> int:
    """纯 Python 浮点运算 + PRNG，返回圆内点数"""
    inside = 0
    r = random.Random()
    for _ in range(iterations):
        x = r.random()
        y = r.random()
        if x * x + y * y < 1.0:
            inside += 1
    return inside


def bench_monte_carlo() -> list[tuple[int, float]]:
    ITER = 8_000_000  # 每个线程的迭代次数
    print(f"\n--- 蒙特卡洛 π (每线程 {ITER:,} 次迭代) ---")
    results = []
    for n in [1, 2, 4, 8]:
        inside_total = 0

        def worker():
            nonlocal inside_total
            inside_total += _monte_carlo_pi(ITER)

        elapsed = _run_threads(worker, n)
        pi_est = 4.0 * inside_total / (ITER * n)
        results.append((n, elapsed))
        if n == 1:
            t1 = elapsed
            speedup = 1.0
        else:
            speedup = t1 / elapsed
        print(f"  {n:2d} 线程: {elapsed:.3f}s  π≈{pi_est:.6f}  (speedup: {speedup:.2f}x)")
    return results


# ============================================================
# 任务 2：纯 Python 矩阵乘法
# ============================================================
def _matmul(N: int) -> list[list[float]]:
    """纯 Python 三层循环 NxN 矩阵乘法"""
    A = [[(i + j) * 0.01 for j in range(N)] for i in range(N)]
    B = [[(i - j) * 0.01 for j in range(N)] for i in range(N)]
    C = [[0.0] * N for _ in range(N)]
    for i in range(N):
        ai = A[i]
        ci = C[i]
        for k in range(N):
            aik = ai[k]
            bk = B[k]
            for j in range(N):
                ci[j] += aik * bk[j]
    return C


def bench_matmul():
    N = 160
    print(f"\n--- 矩阵乘法 {N}x{N} (纯 Python 三层循环) ---")
    for n in [1, 2, 4, 8]:
        elapsed = _run_threads(lambda: _matmul(N), n)
        if n == 1:
            t1 = elapsed
            speedup = 1.0
        else:
            speedup = t1 / elapsed
        print(f"  {n:2d} 线程: {elapsed:.3f}s  (speedup: {speedup:.2f}x)")


# ============================================================
# 任务 3：Mandelbrot 集合
# ============================================================
def bench_mandelbrot():
    W, H, ITER = 800, 600, 256
    print(f"\n--- Mandelbrot {W}x{H}, max_iter={ITER} ---")

    def _compute(width: int, height: int, y_start: int, y_end: int, max_iter: int) -> int:
        total = 0
        for y in range(y_start, y_end):
            cy = -1.5 + 2.0 * y / height
            for x in range(width):
                cx = -2.0 + 3.0 * x / width
                zx = zy = 0.0
                n = 0
                while n < max_iter and zx * zx + zy * zy < 4.0:
                    zx, zy = zx * zx - zy * zy + cx, 2.0 * zx * zy + cy
                    n += 1
                total += n
        return total

    for n in [1, 2, 4, 8]:
        results = [0] * n

        def worker(tid: int):
            y_start = H * tid // n
            y_end = H * (tid + 1) // n
            results[tid] = _compute(W, H, y_start, y_end, ITER)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        t0 = time.perf_counter()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.perf_counter() - t0
        checksum = sum(results)
        if n == 1:
            t1 = elapsed
            speedup = 1.0
        else:
            speedup = t1 / elapsed
        print(f"  {n:2d} 线程: {elapsed:.3f}s  checksum={checksum:,}  (speedup: {speedup:.2f}x)")


# ============================================================
# 运行
# ============================================================
if __name__ == "__main__":
    has_gil = True
    if hasattr(sys, '_is_gil_enabled'):
        has_gil = sys._is_gil_enabled()

    print(f"Python {sys.version.split()[0]}  |  GIL: {'ON' if has_gil else 'OFF (free-threaded)'}")
    print("=" * 55)

    bench_monte_carlo()
    bench_matmul()
    bench_mandelbrot()

    print("=" * 55)
