from glycan_profiling.cli import (
    base, build_db, inspect, mzml, analyze, config)

try:
    from glycresoft_app import server
except ImportError as e:
    import traceback
    traceback.print_exc()
    pass

main = base.cli.main

if __name__ == '__main__':
    main()
