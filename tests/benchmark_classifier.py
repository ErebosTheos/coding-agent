import time
import re
from codegen_agent.classifier import classify_failure, FailureType

# Mock data for benchmarking
STDOUT_LINT = """error: incompatible types in assignment (expression has type "int", variable has type "str")
Found 1 error in 1 file (checked 1 source file)"""
STDOUT_TEST = """Traceback (most recent call last):
  File "test_main.py", line 5, in <module>
    assert add(1, 2) == 4
AssertionError"""
STDOUT_RUNTIME = """Traceback (most recent call last):
  File "main.py", line 10, in <module>
    raise RuntimeError("Something went wrong")
RuntimeError: Something went wrong"""

def benchmark_classifier():
    iterations = 10000
    
    print(f"Running benchmark with {iterations} iterations...")
    
    start_time = time.perf_counter()
    for _ in range(iterations):
        classify_failure("mypy .", STDOUT_LINT)
        classify_failure("pytest", STDOUT_TEST)
        classify_failure("python main.py", STDOUT_RUNTIME)
        classify_failure("unknown_cmd", "some random output")
    end_time = time.perf_counter()
    
    total_time = end_time - start_time
    avg_time = (total_time / (iterations * 4)) * 1000000 # microseconds
    
    print(f"Total time: {total_time:.4f} seconds")
    print(f"Average time per classification: {avg_time:.2f} microseconds")

if __name__ == "__main__":
    benchmark_classifier()
