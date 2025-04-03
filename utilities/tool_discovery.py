# In a new file named tool_discovery.py
import importlib
import pkgutil
import inspect
import os
import sys

def import_submodules(package_name):
    """Import all submodules of a package."""
    package = __import__(package_name)
    
    for _, name, is_pkg in pkgutil.iter_modules(package.__path__, package.__name__ + '.'):
        if not is_pkg:
            importlib.import_module(name)
        else:
            import_submodules(name)

def discover_tools(agents_folder='agents'):
    """Discover and import all agent modules to register their tools."""
    # Add the current directory to sys.path if needed
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if current_dir not in sys.path:
        sys.path.insert(0, current_dir)
    
    # Import all modules in the agents folder
    import_submodules(agents_folder)
    import_submodules('utilities')
    
    # You can also scan specific folders for Python files and import them
    # For example:
    # for root, dirs, files in os.walk(agents_folder):
    #     for file in files:
    #         if file.endswith('.py') and not file.startswith('__'):
    #             module_path = os.path.join(root, file)[:-3].replace(os.sep, '.')
    #             importlib.import_module(module_path)