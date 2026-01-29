# High-Performance Python: Zero-GC Design and Memory Optimization

A comprehensive guide to building high-performance Python systems with minimal garbage collection overhead, focusing on memory allocations, zero-GC patterns, and predictable memory behavior.

---

## Table of Contents

1. [Python Memory & GC Fundamentals](#1-python-memory--gc-fundamentals)
2. [Allocation Is the Enemy: Minimizing Object Creation](#2-allocation-is-the-enemy-minimizing-object-creation)
3. [Zero-GC Data Structure Design](#3-zero-gc-data-structure-design)
4. [GC-Free Programming Patterns](#4-gc-free-programming-patterns)
5. [Disabling or Controlling GC Safely](#5-disabling-or-controlling-gc-safely)
6. [High-Performance Containers & Alternatives](#6-high-performance-containers--alternatives)
7. [C Extensions, CFFI, and Native Memory](#7-c-extensions-cffi-and-native-memory)
8. [NumPy & Zero-GC Thinking](#8-numpy--zero-gc-thinking)
9. [Concurrency Without GC Pain](#9-concurrency-without-gc-pain)
10. [Profiling for Allocation & GC](#10-profiling-for-allocation--gc)
11. [Case Studies](#11-case-studies)
12. [Design Tradeoffs](#12-design-tradeoffs--when-not-to-use-zero-gc)

---

## 1. Python Memory & GC Fundamentals

> **Key Principle**: Zero-GC ≠ no memory management. It means **predictable memory behavior**.

### 1.1 CPython Memory Model Overview

CPython uses a **two-pronged memory management strategy**:

1. **Reference Counting** (primary mechanism)
2. **Cyclic Garbage Collector** (supplementary for cyclic references)

#### Reference Counting

Every Python object contains a reference count (`ob_refcnt`) that tracks how many references point to it:

```c
// Simplified CPython object header
struct PyObject {
    Py_ssize_t ob_refcnt;  // Reference count
    PyTypeObject *ob_type; // Type pointer
};
```

**How it works:**
- When a reference is created → count increments
- When a reference is deleted → count decrements
- When count reaches zero → object is **immediately** deallocated

```python
import sys

a = [1, 2, 3]
print(sys.getrefcount(a))  # Shows refcount (includes temporary from function call)

b = a  # Increment refcount
del a  # Decrement refcount (object still exists)
del b  # Object deallocated immediately
```

**Pros:**
- Deterministic: Objects freed immediately when unreachable
- No stop-the-world pauses (for non-cyclic objects)
- Simple mental model

**Cons:**
- Cannot detect cyclic references
- Every read operation writes to `ob_refcnt` (causes Copy-on-Write issues in forked processes)
- Overhead on every reference operation

#### Cyclic Garbage Collector

Reference counting fails when objects form cycles:

```python
# Cyclic reference - refcount never reaches zero
a = []
b = []
a.append(b)
b.append(a)
del a, b  # Objects still exist! Refcounts are both 1
```

The cyclic GC uses a **generational** approach to detect and collect these cycles.

### 1.2 Generational Garbage Collector

CPython's GC divides objects into **four generations** (as of Python 3.13+):

| Generation | Purpose | Collection Frequency |
|------------|---------|---------------------|
| Generation 0 (Young) | New objects | Most frequent |
| Generation 1 (Old) | Survived Gen 0 | Less frequent |
| Generation 2 (Older) | Survived Gen 1 | Least frequent |
| Permanent | Static/frozen objects | Never collected |

```python
import gc

# View current thresholds (threshold0, threshold1, threshold2)
print(gc.get_threshold())  # Default: (700, 10, 10)

# Meaning:
# - Gen 0 collection triggers when allocations - deallocations > 700
# - Gen 1 collection triggers every 10 Gen 0 collections
# - Gen 2 collection triggers every 10 Gen 1 collections
```

#### When GC Runs

1. **Threshold-based**: When object count in a generation exceeds threshold
2. **Manual**: When `gc.collect()` is called
3. **Interpreter shutdown**: Final cleanup

```python
import gc

# Get current counts per generation
print(gc.get_count())  # (count0, count1, count2)

# Get detailed statistics
for i, stats in enumerate(gc.get_stats()):
    print(f"Gen {i}: collections={stats['collections']}, "
          f"collected={stats['collected']}, uncollectable={stats['uncollectable']}")
```

### 1.3 Cost of GC Pauses

**GC is a stop-the-world event**:

```
Timeline:
  [...processing...] | GC PAUSE | [...processing...]
                      ↑
            Your network card is buffering packets
            Market has moved
            Users are waiting
```

**Real-world impact** (from high-frequency trading systems):
- Median tick-to-trade: 50μs
- p99.9 latency WITHOUT GC control: 15ms (300x spike!)
- p99.9 latency WITH GC control: ~80μs

### 1.4 The PyMalloc Allocator

CPython uses a **three-tier** allocation hierarchy:

```
┌─────────────────────────────────────────────────────┐
│                    ARENAS (256 KB)                   │
│  ┌───────────────────────────────────────────────┐  │
│  │              POOLS (4 KB each)                │  │
│  │  ┌─────────────────────────────────────────┐  │  │
│  │  │        BLOCKS (8-512 bytes)             │  │  │
│  │  │  [obj][obj][obj][obj][obj][obj][obj]    │  │  │
│  │  └─────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────┘
```

**Key characteristics:**
- Arenas: 256 KB chunks from OS via `mmap`
- Pools: 4 KB, contain blocks of ONE size class
- Blocks: 8-512 bytes (8-byte aligned)
- Objects > 512 bytes → direct `malloc`

**Critical insight**: Arenas are only released to the OS when **ALL** pools within them are empty. Long-running processes often hold fragmented arenas.

```python
import sys
# View allocation statistics
sys._debugmallocstats()
```

### 1.5 Python 3.13+ Changes: Free-Threaded Build

Python 3.13 introduced an **experimental free-threaded build** (no GIL):

| Feature | Default Build | Free-Threaded Build |
|---------|--------------|---------------------|
| GIL | Required | Optional |
| GC Algorithm | Generational | Non-generational |
| Object Header | Standard | Includes `ob_gc_bits` |
| Thread Safety | GIL-based | Fine-grained locks |

```bash
# Build Python without GIL
./configure --disable-gil
make
```

**Free-threaded GC differences:**
- Pauses ALL threads during collection (for thread safety)
- Memory pressure-based triggers (RSS monitoring)
- Can skip collections if memory hasn't grown

---

## 2. Allocation Is the Enemy: Minimizing Object Creation

> **Core Truth**: High-performance Python is about **not allocating**.

### 2.1 Hot-Path Allocation Analysis

Identify allocations in performance-critical code:

```python
import tracemalloc

tracemalloc.start()

# Your hot path code
result = process_data(data)

snapshot = tracemalloc.take_snapshot()
top_stats = snapshot.statistics('lineno')

for stat in top_stats[:10]:
    print(stat)
```

### 2.2 Common Hidden Allocations

#### Tuple Creation in Function Calls

```python
# BAD: Creates tuple on every call
def process(x, y, z):
    return func(x, y, z)  # Tuple created for *args

# BETTER: Use direct arguments or slots
```

#### String Concatenation

```python
# BAD: Creates new string each iteration
result = ""
for item in items:
    result += str(item)  # O(n²) allocations!

# GOOD: Single allocation
result = "".join(str(item) for item in items)

# BEST for known format: f-strings (single allocation)
result = f"{a}-{b}-{c}"
```

#### Short-Lived Lists/Dicts

```python
# BAD: Creates temporary list
for i in range(len(items)):
    process(items[i])

# GOOD: Direct iteration
for item in items:
    process(item)

# BAD: Creates list from range
for i in list(range(1000)):
    pass

# GOOD: Range is an iterator
for i in range(1000):
    pass
```

#### Implicit Object Creation

```python
# BAD: Creates new dict each call
def get_config():
    return {"timeout": 30, "retries": 3}

# GOOD: Module-level constant
_CONFIG = {"timeout": 30, "retries": 3}
def get_config():
    return _CONFIG

# Or use __slots__ class for type safety
class Config:
    __slots__ = ('timeout', 'retries')
    def __init__(self):
        self.timeout = 30
        self.retries = 3
```

### 2.3 Expression-Level Optimizations

#### Local Variable Binding

```python
# BAD: Global lookup each iteration
for item in items:
    result = math.sin(item)  # Global lookup: math, then sin

# GOOD: Bind locally
sin = math.sin  # Single global lookup
for item in items:
    result = sin(item)  # Local lookup only
```

#### In-Place Operations

```python
# Creates new object
x = x + y

# Modifies in place (when possible)
x += y

# For lists:
my_list = my_list + [item]  # BAD: new list
my_list += [item]           # Better: extend in place
my_list.append(item)        # BEST: single item
```

#### Iterator Reuse

```python
# BAD: Creates new iterator each call
def get_first_match(items, predicate):
    return next(filter(predicate, items), None)

# GOOD: Reuse iterator pattern
def get_first_match(items, predicate):
    for item in items:
        if predicate(item):
            return item
    return None
```

### 2.4 Interned Objects

Python automatically interns certain objects:

```python
# Small integers (-5 to 256) are interned
a = 256
b = 256
print(a is b)  # True - same object

a = 257
b = 257
print(a is b)  # False - different objects (usually)

# Short strings without special chars are interned
a = "hello"
b = "hello"
print(a is b)  # True

# Force string interning
import sys
a = sys.intern("my_frequently_used_string")
```

---

## 3. Zero-GC Data Structure Design

> **Goal**: Design structures that **never create garbage** in steady state.

### 3.1 Pre-Allocated Buffers

```python
import array

class FixedBuffer:
    """Pre-allocated buffer that reuses memory."""

    __slots__ = ('_buffer', '_size', '_position')

    def __init__(self, capacity: int):
        # Single allocation at construction
        self._buffer = array.array('d', [0.0]) * capacity
        self._size = capacity
        self._position = 0

    def write(self, value: float) -> bool:
        if self._position >= self._size:
            return False
        self._buffer[self._position] = value
        self._position += 1
        return True

    def reset(self):
        """Reset without deallocating."""
        self._position = 0
```

### 3.2 Ring Buffers / Circular Queues

```python
class RingBuffer:
    """Fixed-size circular buffer - zero steady-state allocations."""

    __slots__ = ('_buffer', '_capacity', '_head', '_tail', '_size')

    def __init__(self, capacity: int):
        self._buffer = [None] * capacity  # Single allocation
        self._capacity = capacity
        self._head = 0  # Read position
        self._tail = 0  # Write position
        self._size = 0

    def push(self, item) -> bool:
        """Add item. Returns False if full."""
        if self._size >= self._capacity:
            return False
        self._buffer[self._tail] = item
        self._tail = (self._tail + 1) % self._capacity
        self._size += 1
        return True

    def pop(self):
        """Remove and return oldest item."""
        if self._size == 0:
            return None
        item = self._buffer[self._head]
        self._buffer[self._head] = None  # Allow GC of item
        self._head = (self._head + 1) % self._capacity
        self._size -= 1
        return item

    def __len__(self):
        return self._size
```

### 3.3 Object Pools

```python
class ObjectPool:
    """Reuses objects instead of allocating/deallocating."""

    __slots__ = ('_factory', '_pool', '_max_size')

    def __init__(self, factory, initial_size: int = 10, max_size: int = 100):
        self._factory = factory
        self._max_size = max_size
        # Pre-allocate objects
        self._pool = [factory() for _ in range(initial_size)]

    def acquire(self):
        """Get an object from pool or create new if empty."""
        if self._pool:
            return self._pool.pop()
        return self._factory()

    def release(self, obj):
        """Return object to pool for reuse."""
        if len(self._pool) < self._max_size:
            # Reset object state here if needed
            self._pool.append(obj)
        # Else: let GC handle overflow


# Usage
class Message:
    __slots__ = ('type', 'payload', 'timestamp')

    def reset(self):
        self.type = None
        self.payload = None
        self.timestamp = 0

message_pool = ObjectPool(Message, initial_size=1000)

# Hot path - no allocations!
msg = message_pool.acquire()
msg.type = "TRADE"
msg.payload = data
msg.timestamp = time.time()
process(msg)
msg.reset()
message_pool.release(msg)
```

### 3.4 Free Lists

```python
class FreeListAllocator:
    """Slab-style allocator maintaining a free list."""

    __slots__ = ('_allocated', '_free_list', '_factory')

    def __init__(self, factory, prealloc: int = 100):
        self._factory = factory
        self._allocated = []
        self._free_list = []

        # Pre-allocate slab
        for _ in range(prealloc):
            obj = factory()
            self._allocated.append(obj)
            self._free_list.append(obj)

    def alloc(self):
        if self._free_list:
            return self._free_list.pop()
        # Grow allocation
        obj = self._factory()
        self._allocated.append(obj)
        return obj

    def free(self, obj):
        self._free_list.append(obj)
```

### 3.5 Struct-of-Arrays vs Array-of-Structs

```python
import array

# Array-of-Structs (typical Python)
# Each particle is a separate object -> many allocations
class Particle:
    __slots__ = ('x', 'y', 'vx', 'vy', 'mass')

particles = [Particle() for _ in range(10000)]  # 10000 objects!


# Struct-of-Arrays (zero-GC friendly)
# Single allocation per property
class ParticleSystem:
    __slots__ = ('x', 'y', 'vx', 'vy', 'mass', '_count')

    def __init__(self, capacity: int):
        self.x = array.array('d', [0.0]) * capacity
        self.y = array.array('d', [0.0]) * capacity
        self.vx = array.array('d', [0.0]) * capacity
        self.vy = array.array('d', [0.0]) * capacity
        self.mass = array.array('d', [0.0]) * capacity
        self._count = 0

    def add_particle(self, x, y, vx, vy, mass):
        i = self._count
        self.x[i] = x
        self.y[i] = y
        self.vx[i] = vx
        self.vy[i] = vy
        self.mass[i] = mass
        self._count += 1

    def update(self, dt):
        """Vectorizable, cache-friendly update."""
        for i in range(self._count):
            self.x[i] += self.vx[i] * dt
            self.y[i] += self.vy[i] * dt

# 5 array allocations vs 10000 object allocations!
system = ParticleSystem(10000)
```

---

## 4. GC-Free Programming Patterns

### 4.1 "Allocate Once, Reuse Forever"

```python
class MessageProcessor:
    """Processor that allocates all resources at init."""

    __slots__ = ('_buffer', '_scratch', '_results')

    def __init__(self, max_message_size: int, batch_size: int):
        # All allocations happen ONCE
        self._buffer = bytearray(max_message_size)
        self._scratch = bytearray(max_message_size)
        self._results = [None] * batch_size

    def process(self, data: bytes):
        """Process without allocating."""
        # Reuse pre-allocated buffers
        self._buffer[:len(data)] = data
        # ... process using self._scratch ...
        return self._results  # Return reference to existing array
```

### 4.2 Phase-Based Allocation

```python
class TradingEngine:
    """Separates allocation phase from hot path."""

    def __init__(self):
        self._initialized = False
        self._order_pool = None
        self._message_buffer = None

    def initialize(self, config):
        """ALLOCATION PHASE - run during startup."""
        import gc
        gc.disable()  # Disable during hot init

        self._order_pool = ObjectPool(Order, initial_size=config.max_orders)
        self._message_buffer = RingBuffer(config.message_queue_size)

        # Force collection of init garbage
        gc.enable()
        gc.collect()
        gc.freeze()  # Move to permanent generation

        self._initialized = True

    def process_tick(self, tick):
        """HOT PATH - zero allocations."""
        assert self._initialized
        # All objects come from pools
        order = self._order_pool.acquire()
        # ... process ...
        self._order_pool.release(order)
```

### 4.3 Manual Reset Instead of Destroy/Recreate

```python
class ReusableParser:
    """Parser that resets state instead of recreating."""

    __slots__ = ('_state', '_buffer', '_position', '_result')

    def __init__(self, buffer_size: int):
        self._state = 0
        self._buffer = bytearray(buffer_size)
        self._position = 0
        self._result = {}

    def reset(self):
        """Reset for reuse - no allocations."""
        self._state = 0
        self._position = 0
        self._result.clear()  # Reuse dict, don't recreate

    def parse(self, data: bytes):
        self.reset()  # Reuse instead of recreate
        # ... parse logic ...
        return self._result  # Return same dict reference
```

### 4.4 Avoiding Cyclic References by Design

```python
import weakref

# BAD: Cyclic reference
class Parent:
    def __init__(self):
        self.children = []

class Child:
    def __init__(self, parent):
        self.parent = parent  # Strong reference back

# GOOD: Weak reference breaks cycle
class Child:
    def __init__(self, parent):
        self._parent_ref = weakref.ref(parent)

    @property
    def parent(self):
        return self._parent_ref()


# BETTER: No back-reference needed
class ParentWithRegistry:
    _children_by_id = {}  # External registry

    def __init__(self):
        self.children = []

    def add_child(self, child):
        child_id = id(child)
        self._children_by_id[child_id] = self
        self.children.append(child_id)

    @classmethod
    def get_parent(cls, child):
        return cls._children_by_id.get(id(child))
```

### 4.5 Immutability vs Mutability for Performance

```python
# Immutable: Safe but allocates on every "change"
Point = namedtuple('Point', ['x', 'y'])
p1 = Point(1, 2)
p2 = Point(p1.x + 1, p1.y)  # New allocation

# Mutable: Dangerous but zero allocations
class MutablePoint:
    __slots__ = ('x', 'y')

    def __init__(self, x, y):
        self.x = x
        self.y = y

    def translate(self, dx, dy):
        self.x += dx  # In-place modification
        self.y += dy
        return self  # For chaining

# Choose based on context:
# - Immutable for API boundaries, shared state
# - Mutable for hot paths with single ownership
```

---

## 5. Disabling or Controlling GC Safely

### 5.1 When It's Safe to Disable GC

**Safe scenarios:**
- Short-lived scripts that exit before memory pressure
- Worker processes that restart frequently
- Code paths with NO cyclic references
- Sections using only pre-allocated objects

**Dangerous scenarios:**
- Long-running services without memory monitoring
- Code using third-party libraries that create cycles
- Systems where memory leaks are unacceptable

### 5.2 GC Control Strategies

#### Complete Disable (Use with caution!)

```python
import gc

# Method 1: gc.disable()
gc.disable()

# Method 2: Zero threshold (more robust)
# Some libraries call gc.enable(), but this survives:
gc.set_threshold(0)

# Instagram's approach - immune to gc.enable() calls
gc.set_threshold(0, 0, 0)
```

#### Selective Disable for Hot Paths

```python
import gc
from contextlib import contextmanager

@contextmanager
def no_gc():
    """Temporarily disable GC for critical section."""
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if gc_was_enabled:
            gc.enable()

# Usage
with no_gc():
    # Time-critical code here
    process_market_data(tick)
```

#### Scheduled GC During Quiet Periods

```python
import gc
import time

class GCScheduler:
    """Run GC during known quiet periods."""

    def __init__(self, quiet_period_seconds: float = 0.001):
        self._quiet_period = quiet_period_seconds
        self._last_gc = time.time()
        gc.disable()

    def maybe_collect(self):
        """Call during idle moments."""
        now = time.time()
        if now - self._last_gc > self._quiet_period:
            gc.collect(generation=0)  # Quick gen-0 only
            self._last_gc = now

    def full_collect(self):
        """Call during maintenance windows."""
        gc.collect(generation=2)
        self._last_gc = time.time()


# Usage in main loop
scheduler = GCScheduler()

while True:
    message = queue.get(timeout=0.0001)  # Non-blocking
    if message:
        process(message)
    else:
        scheduler.maybe_collect()  # Idle moment
```

### 5.3 Using gc.freeze() for Fork Safety

```python
import gc
import os

def fork_safe_init():
    """Prepare for fork() to maximize shared memory."""
    # 1. Disable GC in parent
    gc.disable()

    # 2. Do all initialization
    app = initialize_app()
    load_models()
    warm_caches()

    # 3. Force final collection
    gc.collect()
    gc.collect()
    gc.collect()

    # 4. Freeze current objects (Python 3.7+)
    gc.freeze()

    # 5. Fork workers
    for _ in range(num_workers):
        if os.fork() == 0:
            # Child process
            gc.enable()  # Re-enable in child
            run_worker()
            break
        # Parent continues
```

### 5.4 Monitoring GC Behavior

```python
import gc

# Register callback for GC events
def gc_callback(phase, info):
    if phase == 'start':
        print(f"GC starting: gen={info['generation']}")
    elif phase == 'stop':
        print(f"GC finished: collected={info['collected']}, "
              f"uncollectable={info['uncollectable']}")

gc.callbacks.append(gc_callback)

# Context manager for measuring GC pauses
import time

@contextmanager
def measure_gc():
    """Measure time spent in GC during code block."""
    gc_time = [0]
    start_gen_stats = [s.copy() for s in gc.get_stats()]

    def callback(phase, info):
        if phase == 'start':
            gc_time.append(-time.perf_counter())
        elif phase == 'stop':
            gc_time[-1] += time.perf_counter()

    gc.callbacks.append(callback)
    try:
        yield gc_time
    finally:
        gc.callbacks.remove(callback)
        print(f"Total GC time: {sum(gc_time):.6f}s")
```

---

## 6. High-Performance Containers & Alternatives

### 6.1 Container Performance Comparison

| Container | Memory | Index Access | Append | Pop(0) | Use Case |
|-----------|--------|--------------|--------|--------|----------|
| `list` | Higher | O(1) | O(1)* | O(n) | General purpose |
| `array.array` | Lower | O(1) | O(1)* | O(n) | Homogeneous numeric |
| `bytearray` | Lower | O(1) | O(1)* | O(n) | Binary data |
| `deque` | Medium | O(n) | O(1) | O(1) | Queue/stack |
| `numpy.ndarray` | Lowest | O(1) | N/A | N/A | Numeric computation |

\* Amortized, occasional resizing

### 6.2 `array.array` for Typed Data

```python
import array

# Type codes for different data types
# 'b': signed char (1 byte)
# 'i': signed int (4 bytes typically)
# 'l': signed long (8 bytes typically)
# 'd': double (8 bytes)
# 'f': float (4 bytes)

# Memory comparison
import sys

py_list = [0.0] * 1000000
arr = array.array('d', [0.0] * 1000000)

print(f"list size: {sys.getsizeof(py_list):,} bytes")  # ~8MB
print(f"array size: {arr.buffer_info()[1] * 8:,} bytes")  # ~8MB raw data

# But list stores references to float objects!
# Total memory for list: ~8MB (list) + 24MB (float objects) = ~32MB
# Total memory for array: ~8MB (raw doubles)
```

### 6.3 `bytearray` for Binary Data

```python
# Mutable binary buffer
buf = bytearray(1024)

# In-place modification (no allocation)
buf[0:4] = b'\x00\x01\x02\x03'

# Extend without creating intermediates
buf += b'more data'  # In-place extension

# With memoryview for zero-copy slicing
mv = memoryview(buf)
slice_view = mv[10:20]  # No copy! Just a view
slice_view[0] = 0xFF  # Modifies original buf
```

### 6.4 `collections.deque` for Queues

```python
from collections import deque

# Fixed-size queue (auto-discards old items)
bounded_queue = deque(maxlen=1000)

# O(1) operations at both ends
bounded_queue.append(item)      # Add right
bounded_queue.appendleft(item)  # Add left
bounded_queue.pop()             # Remove right
bounded_queue.popleft()         # Remove left

# Rotate efficiently
bounded_queue.rotate(3)   # Move 3 items from right to left
bounded_queue.rotate(-3)  # Move 3 items from left to right
```

### 6.5 Using `__slots__` for Memory Efficiency

```python
import sys

class WithDict:
    def __init__(self, x, y, z):
        self.x = x
        self.y = y
        self.z = z

class WithSlots:
    __slots__ = ('x', 'y', 'z')

    def __init__(self, x, y, z):
        self.x = x
        self.y = y
        self.z = z

# Memory comparison
obj_dict = WithDict(1, 2, 3)
obj_slots = WithSlots(1, 2, 3)

print(f"With __dict__: {sys.getsizeof(obj_dict.__dict__)} bytes for dict")
print(f"With __slots__: no __dict__ overhead")

# For 1 million objects:
# Without __slots__: ~170 MB
# With __slots__: ~70 MB
# Savings: ~60%
```

#### `__slots__` Best Practices

```python
class OptimizedClass:
    __slots__ = ('x', 'y', '_cache')

    # Allow weak references if needed
    # __slots__ = ('x', 'y', '__weakref__')

    # Allow dynamic attributes if needed (defeats purpose)
    # __slots__ = ('x', 'y', '__dict__')

    def __init__(self, x, y):
        self.x = x
        self.y = y
        self._cache = None

# Inheritance with slots
class Base:
    __slots__ = ('base_attr',)

class Derived(Base):
    __slots__ = ('derived_attr',)  # Don't repeat base slots!
```

---

## 7. C Extensions, CFFI, and Native Memory

### 7.1 Why Native Memory?

True zero-GC often means **moving memory out of Python's heap**:

- Native allocations aren't tracked by Python's GC
- No reference counting overhead
- Direct memory control
- Zero-copy interop with C libraries

### 7.2 Comparison of C Integration Approaches

| Approach | Overhead | Ease of Use | PyPy Support | Use Case |
|----------|----------|-------------|--------------|----------|
| ctypes | High | Easy | Yes | Quick prototyping |
| cffi | Low-Medium | Medium | Excellent | Production FFI |
| Cython | Lowest | Hard | No | Performance critical |
| pybind11 | Low | Medium | No | Modern C++ |
| C Extension | Lowest | Hardest | No | Maximum control |

### 7.3 Using cffi for Native Memory

```python
from cffi import FFI

ffi = FFI()

# Allocate native memory (not tracked by Python GC)
native_buffer = ffi.new("double[1000000]")  # 8MB native memory

# Direct manipulation
native_buffer[0] = 3.14159
native_buffer[999999] = 2.71828

# Zero-copy view as Python buffer
buffer_view = ffi.buffer(native_buffer)

# Use with numpy (zero-copy)
import numpy as np
arr = np.frombuffer(buffer_view, dtype=np.float64)

# Memory is freed when native_buffer goes out of scope
# OR explicitly: ffi.release(native_buffer)
```

### 7.4 Buffer Protocol and memoryview

```python
# Any object supporting buffer protocol can be used zero-copy
def process_buffer(data):
    """Works with bytes, bytearray, array.array, numpy, etc."""
    view = memoryview(data)

    # Slice without copying
    header = view[:16]
    payload = view[16:]

    # Modify in place (if writable)
    if not view.readonly:
        view[0] = 0xFF

    return view  # Return view, not copy

# Zero-copy with struct
import struct

def parse_header(buffer):
    """Parse binary header without allocation."""
    view = memoryview(buffer)

    # Unpack from buffer directly
    magic, version, length = struct.unpack_from('<HHI', view)

    return magic, version, length

# Pre-allocated buffer with struct
output_buffer = bytearray(1024)

def write_message(buffer, msg_type, payload):
    """Write to pre-allocated buffer."""
    struct.pack_into('<HH', buffer, 0, msg_type, len(payload))
    buffer[4:4+len(payload)] = payload
```

### 7.5 Cython for Hot Paths

```cython
# fast_math.pyx
cimport cython

@cython.boundscheck(False)
@cython.wraparound(False)
def sum_array(double[:] arr):
    """Sum array with no Python overhead in loop."""
    cdef double total = 0.0
    cdef Py_ssize_t i, n = arr.shape[0]

    for i in range(n):
        total += arr[i]

    return total

# No GC involvement in the hot loop!
```

---

## 8. NumPy & Zero-GC Thinking

### 8.1 Preallocation Strategy

```python
import numpy as np

# BAD: Allocates on every iteration
results = []
for i in range(1000):
    results.append(np.sin(data[i]))
results = np.array(results)  # Another allocation!

# GOOD: Pre-allocate and fill
results = np.empty(1000, dtype=np.float64)
for i in range(1000):
    results[i] = np.sin(data[i])

# BEST: Vectorized (single allocation for result)
results = np.sin(data[:1000])
```

### 8.2 In-Place Operations

```python
import numpy as np

a = np.array([1, 2, 3, 4, 5], dtype=np.float64)
b = np.array([5, 4, 3, 2, 1], dtype=np.float64)

# Creates new array
c = a + b

# In-place (no allocation)
np.add(a, b, out=a)  # a += b

# In-place operations
a *= 2.0       # Multiply in place
a += b         # Add in place
np.exp(a, out=a)  # Exp in place

# Chained operations with pre-allocated output
result = np.empty_like(a)
np.add(a, b, out=result)
np.multiply(result, 2.0, out=result)
np.exp(result, out=result)
```

### 8.3 Views vs Copies

```python
import numpy as np

arr = np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]])

# VIEWS (no copy, share memory)
row_view = arr[0]          # View of first row
col_view = arr[:, 0]       # View of first column
slice_view = arr[1:, 1:]   # View of subarray
transpose = arr.T          # View (transposed)
reshape = arr.reshape(9)   # View (if contiguous)

# COPIES (new allocation)
copy = arr.copy()
fancy_idx = arr[[0, 2]]    # Copy! Fancy indexing
bool_idx = arr[arr > 5]    # Copy! Boolean indexing

# Check if it's a view
print(row_view.base is arr)  # True = view
print(copy.base is arr)      # False = copy

# Ensure no copy with np.asarray
def process(data):
    arr = np.asarray(data)  # No copy if already ndarray
    return arr.sum()
```

### 8.4 Memory Layout Optimization

```python
import numpy as np

# C-contiguous (row-major) - default
c_arr = np.array([[1, 2, 3], [4, 5, 6]], order='C')

# Fortran-contiguous (column-major)
f_arr = np.array([[1, 2, 3], [4, 5, 6]], order='F')

# Check contiguity
print(c_arr.flags['C_CONTIGUOUS'])  # True
print(c_arr.flags['F_CONTIGUOUS'])  # False

# Contiguous is faster for sequential access
# Match layout to access pattern!

# Row-wise operations: use C order
for row in c_arr:  # Fast
    process(row)

# Column-wise operations: use F order
for col in f_arr.T:  # Fast
    process(col)
```

### 8.5 Avoiding Temporary Arrays

```python
import numpy as np

# BAD: Creates 3 temporary arrays
result = a + b + c + d

# BETTER: Chain with explicit output
result = np.empty_like(a)
np.add(a, b, out=result)
np.add(result, c, out=result)
np.add(result, d, out=result)

# GOOD: Use numexpr for complex expressions
import numexpr as ne
result = ne.evaluate("a + b + c + d")  # Single pass, minimal temps

# For matrix operations
# BAD: temporary for (A @ B)
result = (A @ B) @ C

# BETTER: Use out parameter where available
temp = np.empty((A.shape[0], B.shape[1]))
np.dot(A, B, out=temp)
np.dot(temp, C, out=result)
```

---

## 9. Concurrency Without GC Pain

### 9.1 The GIL and GC Interaction

```
Thread 1: [compute][compute][  WAIT FOR GC  ][compute]
Thread 2: [compute][  GC PAUSE   ][compute][compute]
Thread 3: [compute][  GC PAUSE   ][compute][compute]
                   ↑
         ALL threads stop for Gen2 collection
```

### 9.2 Threading vs Multiprocessing GC Costs

| Approach | GC Behavior | Memory | Use Case |
|----------|-------------|--------|----------|
| Threading | Shared GC, GIL contention | Shared | I/O-bound |
| Multiprocessing | Separate GC per process | Duplicated | CPU-bound |
| asyncio | Single-threaded GC | Shared | High-concurrency I/O |

### 9.3 Message Passing with Pre-allocated Buffers

```python
import multiprocessing as mp
from multiprocessing import shared_memory
import numpy as np

def worker(shm_name, shape, dtype, start, end):
    """Worker using shared memory - no serialization overhead."""
    # Attach to existing shared memory
    shm = shared_memory.SharedMemory(name=shm_name)
    arr = np.ndarray(shape, dtype=dtype, buffer=shm.buf)

    # Process in-place
    arr[start:end] *= 2.0

    shm.close()

def main():
    # Create shared memory buffer ONCE
    data = np.random.randn(10_000_000)
    shm = shared_memory.SharedMemory(create=True, size=data.nbytes)

    # Copy data to shared memory
    shared_arr = np.ndarray(data.shape, dtype=data.dtype, buffer=shm.buf)
    shared_arr[:] = data

    # Spawn workers - they access shared memory directly
    processes = []
    chunk_size = len(data) // 4
    for i in range(4):
        start = i * chunk_size
        end = start + chunk_size
        p = mp.Process(target=worker, args=(shm.name, data.shape, data.dtype, start, end))
        processes.append(p)
        p.start()

    for p in processes:
        p.join()

    # Cleanup
    shm.close()
    shm.unlink()
```

### 9.4 Zero-Copy IPC with mmap

```python
import mmap
import os
import struct

class SharedRingBuffer:
    """Lock-free SPSC ring buffer using mmap."""

    HEADER_SIZE = 16  # head, tail pointers

    def __init__(self, path: str, capacity: int, create: bool = False):
        self.capacity = capacity
        self.path = path

        if create:
            # Create file with header + data
            with open(path, 'wb') as f:
                f.write(b'\x00' * (self.HEADER_SIZE + capacity))

        self.fd = os.open(path, os.O_RDWR)
        self.mm = mmap.mmap(self.fd, self.HEADER_SIZE + capacity)

    def _get_head(self) -> int:
        return struct.unpack_from('<Q', self.mm, 0)[0]

    def _set_head(self, val: int):
        struct.pack_into('<Q', self.mm, 0, val)

    def _get_tail(self) -> int:
        return struct.unpack_from('<Q', self.mm, 8)[0]

    def _set_tail(self, val: int):
        struct.pack_into('<Q', self.mm, 8, val)

    def write(self, data: bytes) -> bool:
        """Write data (producer side)."""
        head = self._get_head()
        tail = self._get_tail()

        data_len = len(data)
        if (tail + data_len + 4) % self.capacity == head:
            return False  # Full

        # Write length + data
        offset = self.HEADER_SIZE + tail
        struct.pack_into('<I', self.mm, offset, data_len)
        self.mm[offset + 4:offset + 4 + data_len] = data

        self._set_tail((tail + data_len + 4) % self.capacity)
        return True

    def read(self) -> bytes:
        """Read data (consumer side)."""
        head = self._get_head()
        tail = self._get_tail()

        if head == tail:
            return None  # Empty

        offset = self.HEADER_SIZE + head
        data_len = struct.unpack_from('<I', self.mm, offset)[0]
        data = bytes(self.mm[offset + 4:offset + 4 + data_len])

        self._set_head((head + data_len + 4) % self.capacity)
        return data
```

### 9.5 Async with Allocation Discipline

```python
import asyncio
from dataclasses import dataclass

# Pre-allocate response objects
@dataclass
class Response:
    __slots__ = ('status', 'body', 'headers')
    status: int
    body: bytes
    headers: dict

    def reset(self):
        self.status = 0
        self.body = b''
        self.headers.clear()

class ResponsePool:
    def __init__(self, size: int):
        self._pool = [Response(0, b'', {}) for _ in range(size)]
        self._available = asyncio.Queue()
        for r in self._pool:
            self._available.put_nowait(r)

    async def acquire(self) -> Response:
        return await self._available.get()

    def release(self, response: Response):
        response.reset()
        self._available.put_nowait(response)


# Usage in async handler
response_pool = ResponsePool(1000)

async def handle_request(request):
    response = await response_pool.acquire()
    try:
        response.status = 200
        response.body = process(request)
        response.headers['content-type'] = 'application/json'
        await send_response(response)
    finally:
        response_pool.release(response)
```

---

## 10. Profiling for Allocation & GC

### 10.1 tracemalloc for Allocation Tracking

```python
import tracemalloc
import linecache

def display_top(snapshot, key_type='lineno', limit=10):
    """Display top memory consumers."""
    snapshot = snapshot.filter_traces((
        tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
        tracemalloc.Filter(False, "<unknown>"),
    ))

    top_stats = snapshot.statistics(key_type)

    print(f"Top {limit} lines:")
    for index, stat in enumerate(top_stats[:limit], 1):
        frame = stat.traceback[0]
        print(f"#{index}: {frame.filename}:{frame.lineno}: "
              f"{stat.size / 1024:.1f} KiB")
        line = linecache.getline(frame.filename, frame.lineno).strip()
        if line:
            print(f"    {line}")

# Usage
tracemalloc.start()

# Your code here
result = process_data(data)

snapshot = tracemalloc.take_snapshot()
display_top(snapshot)
```

### 10.2 Comparing Snapshots for Leak Detection

```python
import tracemalloc

tracemalloc.start()

# Baseline
snapshot1 = tracemalloc.take_snapshot()

# Do some work
for i in range(1000):
    process_request(requests[i])

# After work
snapshot2 = tracemalloc.take_snapshot()

# Compare
top_stats = snapshot2.compare_to(snapshot1, 'lineno')

print("Memory changes:")
for stat in top_stats[:10]:
    print(f"{stat.size_diff / 1024:+.1f} KiB: {stat.traceback}")
```

### 10.3 GC Pause Detection

```python
import gc
import time
from collections import deque

class GCMonitor:
    """Monitor GC pauses and collect statistics."""

    def __init__(self, max_history: int = 1000):
        self.pauses = deque(maxlen=max_history)
        self._start_time = None
        gc.callbacks.append(self._callback)

    def _callback(self, phase: str, info: dict):
        if phase == 'start':
            self._start_time = time.perf_counter()
        elif phase == 'stop':
            pause_ms = (time.perf_counter() - self._start_time) * 1000
            self.pauses.append({
                'generation': info['generation'],
                'collected': info['collected'],
                'pause_ms': pause_ms,
                'timestamp': time.time()
            })

    def get_stats(self):
        if not self.pauses:
            return None

        pause_times = [p['pause_ms'] for p in self.pauses]
        return {
            'count': len(pause_times),
            'total_ms': sum(pause_times),
            'avg_ms': sum(pause_times) / len(pause_times),
            'max_ms': max(pause_times),
            'p99_ms': sorted(pause_times)[int(len(pause_times) * 0.99)]
        }


# Usage
monitor = GCMonitor()

# ... run your application ...

stats = monitor.get_stats()
print(f"GC Stats: {stats}")
```

### 10.4 Allocation Rate Monitoring

```python
import gc
import time
import threading

class AllocationMonitor:
    """Monitor object allocation rate."""

    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self._running = False
        self._thread = None
        self._history = []

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join()

    def _monitor_loop(self):
        prev_counts = gc.get_count()
        prev_time = time.time()

        while self._running:
            time.sleep(self.interval)

            curr_counts = gc.get_count()
            curr_time = time.time()

            elapsed = curr_time - prev_time
            allocations_per_sec = [
                (curr_counts[i] - prev_counts[i]) / elapsed
                for i in range(3)
            ]

            self._history.append({
                'timestamp': curr_time,
                'gen0_rate': allocations_per_sec[0],
                'gen1_rate': allocations_per_sec[1],
                'gen2_rate': allocations_per_sec[2]
            })

            prev_counts = curr_counts
            prev_time = curr_time

    def get_history(self):
        return self._history.copy()
```

### 10.5 Memory Profiler Integration

```python
# pip install memory_profiler

from memory_profiler import profile

@profile
def my_function():
    # Your code here
    data = [i ** 2 for i in range(1000000)]
    return sum(data)

# Run with: python -m memory_profiler your_script.py
```

---

## 11. Case Studies

### 11.1 Instagram: Disabling GC at Scale

**Problem:**
- Django web servers using uWSGI with pre-fork mode
- Shared memory between master and workers
- Memory dropped sharply after worker spawn

**Root Cause:**
- Every Python read increments `ob_refcnt` (a write!)
- This triggers Copy-on-Write (CoW)
- GC walks objects, touching refcounts → massive CoW

**Solution:**
```python
import gc
import atexit

# Disable GC with threshold (immune to gc.enable())
gc.set_threshold(0)

# Bypass Python cleanup to avoid final GC
def quick_exit():
    import os
    os._exit(0)

atexit.register(quick_exit)
```

**Results:**
- 8GB RAM freed per server
- 10% CPU throughput improvement (better IPC)
- Later: `gc.freeze()` added to Python 3.7 for proper solution

### 11.2 High-Frequency Trading System

**Problem:**
- Median tick-to-trade: 50μs target
- p99.9 latency: 15ms (300x blowout!)
- Bursts correlated with GC collections

**Solution Architecture:**

```python
class TradingEngine:
    def __init__(self, config):
        # ALLOCATION PHASE
        gc.disable()

        # Pre-allocate everything
        self.order_pool = ObjectPool(Order, 10000)
        self.quote_buffer = RingBuffer(100000)
        self.message_scratch = bytearray(65536)

        # Freeze after init
        gc.collect()
        gc.freeze()

    def on_market_data(self, raw_bytes):
        """HOT PATH - zero allocations."""
        # Parse into pre-allocated buffer
        struct.unpack_from('<qdq', self.message_scratch, 0)

        # Get order from pool
        order = self.order_pool.acquire()
        # ... process ...
        self.order_pool.release(order)

    def maintenance_window(self):
        """Called during market close."""
        gc.collect()  # Safe to collect now
```

**Results:**
- p99.9 latency: 80μs (187x improvement)
- Zero GC pauses during market hours

### 11.3 Real-Time Simulation / Game Loop

**Problem:**
- 60 FPS target = 16.67ms per frame budget
- GC pauses cause visible stuttering
- Memory grows unbounded without collection

**Solution:**

```python
import gc
import time

class GameLoop:
    def __init__(self):
        # Pre-allocate game objects
        self.entity_pool = ObjectPool(Entity, 1000)
        self.particle_system = ParticleSystem(10000)

        # GC scheduling
        self.last_gc = time.time()
        self.gc_interval = 1.0  # Full GC every second
        gc.disable()

    def run(self):
        target_frame_time = 1/60

        while self.running:
            frame_start = time.perf_counter()

            # Update game state (zero allocations)
            self.update()
            self.render()

            # Frame time remaining
            elapsed = time.perf_counter() - frame_start
            remaining = target_frame_time - elapsed

            if remaining > 0.002:  # 2ms spare
                # Quick gen-0 collection in spare time
                gc.collect(0)

            if time.time() - self.last_gc > self.gc_interval:
                # Full collection at end of frame
                gc.collect()
                self.last_gc = time.time()

            # Sleep remaining time
            if remaining > 0:
                time.sleep(remaining)
```

### 11.4 Streaming Parser (Network Packets)

**Problem:**
- High packet rate (100k+ packets/sec)
- Each packet creates parser objects
- Memory pressure and GC storms

**Solution:**

```python
class PacketParser:
    """Zero-allocation packet parser."""

    __slots__ = ('_buffer', '_view', '_offset', '_packet')

    def __init__(self, max_packet_size: int = 65536):
        self._buffer = bytearray(max_packet_size)
        self._view = memoryview(self._buffer)
        self._offset = 0
        self._packet = Packet()  # Reusable packet object

    def parse(self, data: bytes) -> 'Packet':
        """Parse packet without allocations."""
        # Copy to pre-allocated buffer
        data_len = len(data)
        self._view[:data_len] = data

        # Parse header using struct (no allocation)
        header = struct.unpack_from('<HHII', self._view, 0)

        # Reuse packet object
        self._packet.type = header[0]
        self._packet.flags = header[1]
        self._packet.sequence = header[2]
        self._packet.length = header[3]

        # Set payload as view (no copy)
        self._packet.payload_view = self._view[12:12 + header[3]]

        return self._packet


class Packet:
    __slots__ = ('type', 'flags', 'sequence', 'length', 'payload_view')
```

---

## 12. Design Tradeoffs & When NOT to Use Zero-GC

### 12.1 Code Complexity vs Performance

| Approach | Complexity | Benefit | When to Use |
|----------|------------|---------|-------------|
| Standard Python | Low | None | Prototypes, scripts |
| `__slots__` | Low | 40-60% memory | Many object instances |
| Object pools | Medium | Reduce GC pressure | Hot path objects |
| Pre-allocation | Medium | Predictable latency | Real-time systems |
| Native memory | High | True zero-GC | Extreme performance |

### 12.2 When GC is Actually Cheaper

1. **Complex object graphs**: Manual tracking is error-prone
2. **Short-lived processes**: No time for memory accumulation
3. **Development/debugging**: GC finds memory bugs for you
4. **Rapid iteration**: Optimization is premature

### 12.3 Safety Risks of Zero-GC

```python
# RISK 1: Memory leaks
# Without GC, cyclic references NEVER get collected
gc.disable()
a, b = [], []
a.append(b)
b.append(a)
del a, b  # LEAKED! Forever.

# RISK 2: Unbounded growth
# Object pools can grow without bounds
pool = ObjectPool(Message)
for _ in range(1_000_000):
    msg = pool.acquire()
    # Forgot to release!

# RISK 3: Use-after-free
# Object reuse can cause subtle bugs
msg1 = pool.acquire()
msg1.data = "sensitive"
pool.release(msg1)
msg2 = pool.acquire()  # Same object as msg1!
print(msg2.data)  # "sensitive" - data leak!
```

### 12.4 Hybrid Approaches

```python
class HybridSystem:
    """Balanced approach: pools for hot path, GC for rest."""

    def __init__(self):
        # Pool for hot path objects only
        self.message_pool = ObjectPool(Message, 1000)

        # Let GC handle complex objects
        self.config = load_config()  # Normal Python objects
        self.state = {}  # GC-managed dict

        # Controlled GC scheduling
        self.gc_scheduler = GCScheduler(quiet_period=0.1)

    def process_hot_path(self, data):
        """Zero-allocation hot path."""
        msg = self.message_pool.acquire()
        try:
            result = process(msg, data)
            return result
        finally:
            self.message_pool.release(msg)

    def process_cold_path(self, data):
        """GC-managed cold path."""
        # Normal Python - GC handles cleanup
        temp = transform(data)
        result = analyze(temp)
        self.state[data.id] = result
        return result

    def idle(self):
        """Called during quiet periods."""
        self.gc_scheduler.maybe_collect()
```

### 12.5 Decision Framework

**Use Zero-GC patterns when:**
- Latency p99 > p50 by 10x+ due to GC
- Processing > 10k messages/second
- Real-time constraints (games, audio, trading)
- Memory is constrained and predictable
- You can invest in testing and profiling

**Use standard GC when:**
- Latency requirements are relaxed (> 100ms OK)
- Code correctness > performance
- Team is unfamiliar with memory management
- Rapid development is priority
- Application is short-lived

---

## Core Mental Model

> **High-performance Python is about controlling object lifetimes, not micro-optimizing syntax.**

The hierarchy of optimization:

1. **Measure first**: Profile before optimizing
2. **Reduce allocations**: The best GC is no GC
3. **Control GC timing**: Run during quiet periods
4. **Pre-allocate and reuse**: Pools, buffers, views
5. **Native memory**: Last resort for extreme cases

---

## Quick Reference

### GC Control Functions

```python
import gc

gc.disable()              # Disable automatic GC
gc.enable()               # Enable automatic GC
gc.isenabled()            # Check if enabled
gc.collect(generation=2)  # Manual collection
gc.get_count()            # (gen0, gen1, gen2) counts
gc.get_stats()            # Detailed statistics
gc.get_threshold()        # Current thresholds
gc.set_threshold(t0, t1, t2)  # Set thresholds
gc.freeze()               # Move to permanent gen (3.7+)
gc.unfreeze()             # Move back from permanent
gc.get_freeze_count()     # Count of frozen objects
gc.callbacks              # List of GC callbacks
```

### Memory Profiling Tools

| Tool | Purpose | Install |
|------|---------|---------|
| tracemalloc | Allocation tracking | Built-in |
| memory_profiler | Line-by-line memory | pip install |
| pympler | Object size analysis | pip install |
| guppy3/heapy | Heap analysis | pip install |
| memray | Visual profiling | pip install |
| objgraph | Object graph visualization | pip install |

### Performance Containers

```python
from array import array       # Typed arrays
from collections import deque # Double-ended queue
import numpy as np            # Numeric arrays
```

---

## References and Sources

### Official Documentation
- [CPython Garbage Collector Internals](https://github.com/python/cpython/blob/main/InternalDocs/garbage_collector.md)
- [gc module documentation](https://docs.python.org/3/library/gc.html)
- [tracemalloc documentation](https://docs.python.org/3/library/tracemalloc.html)
- [Buffer Protocol](https://docs.python.org/3/c-api/buffer.html)
- [Memory Management in Python](https://docs.python.org/3/c-api/memory.html)

### Case Studies
- [Dismissing Python Garbage Collection at Instagram](https://instagram-engineering.com/dismissing-python-garbage-collection-at-instagram-4dca40b29172)
- [There and Back Again: GC at Instagram (PyCon 2018)](https://pyvideo.org/pycon-us-2018/there-and-back-again-disable-and-re-enable-garbage-collector-at-instagram.html)
- [Taming Python GC for Faster Web Requests](https://making.close.com/posts/taming-the-python-gc/)

### Technical Deep Dives
- [CPython Reference Counting and GC Internals](https://blog.codingconfessions.com/p/cpython-garbage-collection-internals)
- [Python Memory Management](https://rushter.com/blog/python-memory-managment/)
- [Memray - Python Memory Profiler](https://bloomberg.github.io/memray/python_allocators.html)
- [Free-Threaded GC Optimizations in Python 3.14](https://labs.quansight.org/blog/free-threaded-gc-3-14)
- [PEP 703 - Making the GIL Optional](https://peps.python.org/pep-0703/)

### Books and Tutorials
- [Effective Python Item 74: memoryview and bytearray](https://effectivepython.com/2019/10/22/memoryview-bytearray-zero-copy-interactions)
- [Less copies in Python with the buffer protocol](https://eli.thegreenplace.net/2011/11/28/less-copies-in-python-with-the-buffer-protocol-and-memoryviews)
- [Understanding NumPy Internals](https://ipython-books.github.io/45-understanding-the-internals-of-numpy-to-avoid-unnecessary-array-copying/)

### Tools
- [ringbuf - Lock-free ring buffer](https://pypi.org/project/ringbuf/)
- [cffi documentation](https://cffi.readthedocs.io/en/latest/goals.html)
- [memory_profiler](https://pypi.org/project/memory-profiler/)
