import os

EXCLUDE = {'venv', '__pycache__', '.git', 'node_modules', '.pytest_cache','.env'}
EXCLUDE_EXTENSIONS = {'.pyc', '.pyo', '.pyd', '.db', '.sqlite', '.env'}

def build_markdown(root_dir, output_file='project_snapshot.md'):
    lines = ['# Project Snapshot\n']

    # Avoid including the generated snapshot file inside itself.
    output_path = os.path.abspath(output_file)

    for dirpath, dirnames, filenames in os.walk(root_dir):
        # Skip excluded folders
        dirnames[:] = [d for d in sorted(dirnames) if d not in EXCLUDE]
        filenames = sorted(filenames)

        rel = os.path.relpath(dirpath, root_dir)
        depth = 0 if rel == '.' else rel.count(os.sep) + 1
        indent = '  ' * depth
        folder_name = os.path.basename(dirpath) if rel != '.' else os.path.basename(root_dir)

        lines.append(f'{indent}- 📁 **{folder_name}/**\n')

        for filename in filenames:
            if any(filename.endswith(ext) for ext in EXCLUDE_EXTENSIONS):
                continue

            filepath = os.path.join(dirpath, filename)
            if os.path.abspath(filepath) == output_path:
                continue
            file_indent = '  ' * (depth + 1)
            rel_path = os.path.relpath(filepath, root_dir)

            lines.append(f'{file_indent}- 📄 **{filename}**\n')

            try:
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    content = f.read().strip()

                if content:
                    ext = filename.rsplit('.', 1)[-1] if '.' in filename else ''
                    lang = ext if ext not in ('', 'md', 'txt', 'example') else ''
                    lines.append(f'{file_indent}  ```{lang}\n')
                    for line in content.splitlines():
                        lines.append(f'{file_indent}  {line}\n')
                    lines.append(f'{file_indent}  ```\n')
                else:
                    lines.append(f'{file_indent}  *(empty)*\n')

            except Exception as e:
                lines.append(f'{file_indent}  *(could not read: {e})*\n')

    with open(output_file, 'w', encoding='utf-8') as f:
        f.writelines(lines)

    print(f'Done → {output_file}')

if __name__ == '__main__':
    build_markdown('.')