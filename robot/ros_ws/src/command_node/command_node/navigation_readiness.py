def occupancy_grid_is_usable(message) -> bool:
    """Return whether an OccupancyGrid contains one complete nonempty grid."""
    try:
        width = int(message.info.width)
        height = int(message.info.height)
        cell_count = len(message.data)
    except (AttributeError, TypeError, ValueError):
        return False
    return width > 0 and height > 0 and cell_count == width * height
