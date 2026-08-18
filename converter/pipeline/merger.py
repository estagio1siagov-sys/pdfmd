def merge_blocks(markdown_blocks):
    """Junta os blocos de markdown (já convertidos e validados) em um único documento."""
    return "\n\n".join(block.strip() for block in markdown_blocks).strip() + "\n"
