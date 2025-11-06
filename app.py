"""
A PyQt6 application for viewing and tagging pages in a PDF document.

This application allows users to load a PDF, assign "green", "yellow", or "red" tags
to individual pages, filter the view by these tags, and search for text.
Tags are saved to a companion JSON file.
"""

import sys
import json
import fitz  # PyMuPDF
import os
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QLabel, QVBoxLayout, QWidget, QFileDialog,
    QToolBar, QLineEdit, QHBoxLayout, QFrame, QListWidget, QListWidgetItem,
    QSplitter, QToolButton, QCheckBox, QAbstractItemView, QSizePolicy
)

from PyQt6.QtGui import QPixmap, QImage, QKeySequence, QFont, QColor, QIcon, QPainter, QShortcut, QPen, QPalette
from PyQt6.QtCore import Qt, QSize, QEvent, pyqtSignal, QRect

# Define tag colors (hex codes for CSS/style)
TAG_COLORS = {
    "green": "#4CAF50",  # "known"
    "yellow": "#FFEB3B", # "review"
    "red": "#F44336",   # "hard"
    "none": "#333333"   # Default no tag
}
# Define tag colors (QColor objects for QPainter)
TAG_COLORS_BG = {
    "green": QColor("#4CAF50"),
    "yellow": QColor("#FFEB3B"),
    "red": QColor("#F44336"),
    "none": QColor("#444444")
}

class PDFPageView(QLabel):
    """
    A custom QLabel widget designed to render a single PDF page
    and handle mouse events for text selection.
    """
    selectionChanged = pyqtSignal() # Signal emitted when text selection changes

    def leaveEvent(self, event):
        """Handle mouse leaving the widget."""
        self.setCursor(Qt.CursorShape.ArrowCursor)
        return super().leaveEvent(event)

    def __init__(self, parent=None):
        """Initialize the PDF page view."""
        super().__init__(parent)
        self.setMouseTracking(True) # Enable mouse move events even when no button is pressed
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus) # Widget can receive keyboard focus
        self._page = None        # The current fitz.Page object
        self._pixmap = None      # The cached QPixmap render of the page
        self._zoom = 1.0         # Current zoom level (calculated)
        self._selecting = False  # Flag for active mouse selection
        self._selection = None   # The QRect of the current selection (in widget coords)
        self._words = []         # List of words on the page from fitz
        self._sel_word_rects = [] # List of QRects for *selected* words
        self._word_rects_widget = [] # List of QRects for *all* words (in widget coords)

    def select_all_text(self):
        """Selects all words on the current page."""
        if not self._page or not self._words:
            return
        # Set selection rectangle to the entire widget
        self._selection = QRect(0, 0, self.width(), self.height())
        # Convert all word rects from page coordinates to widget coordinates
        self._sel_word_rects = [self.page_rect_to_widget_rect(fitz.Rect(x0, y0, x1, y1))
                                for (x0, y0, x1, y1, *_ ) in self._words]
        self.selectionChanged.emit() # Notify that selection has changed
        self.update() # Trigger a repaint

    def rebuild_word_widget_rects(self, target: QRect):
        """
        Updates the cache of word rectangles in widget coordinates,
        focusing only on words visible within the target QRect.
        """
        self._word_rects_widget = []
        if not self._page or target.isEmpty() or not self._words:
            return
        rects = []
        for w in self._words:
            x0, y0, x1, y1, *_ = w
            wr = fitz.Rect(x0, y0, x1, y1) # Page coordinates
            wr_qt = self.page_rect_to_widget_rect(wr) # Widget coordinates
            if wr_qt.intersects(target): # Only cache visible/nearby words
                rects.append(wr_qt)
        self._word_rects_widget = rects

    def set_page(self, page):
        """
        Sets a new fitz.Page to be displayed and resets widget state.
        """
        self._page = page
        # Invalidate cached pixmap and selection
        self._pixmap = None
        self._selection = None
        self._sel_word_rects = []
        try:
            # Extract word information for text selection
            self._words = page.get_text("words")
        except Exception:
            self._words = [] # Handle pages with no text
        self.update() # Trigger repaint
        self._word_rects_widget = [] # Clear word rect cache

    def has_selection(self):
        """Checks if there is an active selection rectangle."""
        return self._selection is not None and not self._selection.isEmpty()

    def clear_selection(self):
        """Clears the current text selection."""
        self._selection = None
        self._sel_word_rects = []
        self.selectionChanged.emit()
        self.update()

    def page_size_pts(self):
        """Returns the page width and height in points (PDF coordinates)."""
        if not self._page:
            return 1.0, 1.0
        r = self._page.rect
        return float(r.width), float(r.height)

    def image_draw_rect(self):
        """
        Calculates the QRect (in widget coordinates) where the page pixmap
        should be drawn, maintaining aspect ratio and adding a margin.
        """
        if not self._page:
            return QRect()
        margin = 10
        avail = self.rect().adjusted(margin, margin, -margin, -margin) # Available space
        pw, ph = self.page_size_pts()
        pm_ratio = pw / max(1.0, ph) # Page aspect ratio
        box_ratio = avail.width() / max(1, avail.height()) # Widget aspect ratio

        # Fit to width or height to maintain aspect ratio
        if pm_ratio > box_ratio:
            # Fit to width
            w = avail.width()
            h = int(w / pm_ratio)
            x = avail.left()
            y = avail.top() + (avail.height() - h) // 2
        else:
            # Fit to height
            h = avail.height()
            w = int(h * pm_ratio)
            x = avail.left() + (avail.width() - w) // 2
            y = avail.top()
        return QRect(x, y, w, h)

    def ensure_pixmap_for_target(self, target: QRect):
        """
        Renders the PDF page to a QPixmap if the cache is invalid.
        Calculates the required zoom based on the target draw rectangle.
        """
        if not self._page or target.isEmpty():
            self._pixmap = None
            return
        
        dpr = self.devicePixelRatioF() # Handle high-DPI displays
        pw, ph = self.page_size_pts()
        
        # Calculate render size based on target rect and DPI
        render_w = max(1, int(target.width() * dpr))
        render_h = max(1, int(target.height() * dpr))
        
        # Calculate zoom needed to fit the page to the render size
        zoom_x = render_w / max(1.0, pw)
        zoom_y = render_h / max(1.0, ph)
        zoom = min(zoom_x, zoom_y) # Use smallest zoom to fit
        self._zoom = zoom
        
        # Render using fitz
        mat = fitz.Matrix(zoom, zoom)
        pix = self._page.get_pixmap(matrix=mat, alpha=False)
        
        # Convert fitz pixmap to QImage
        img_format = QImage.Format.Format_RGB888 if pix.alpha == 0 else QImage.Format.Format_RGBA8888
        qimage = QImage(pix.samples, pix.width, pix.height, pix.stride, img_format)
        
        # Convert QImage to QPixmap and set DPI
        pm = QPixmap.fromImage(qimage)
        pm.setDevicePixelRatio(dpr)
        self._pixmap = pm
        
        # Update word rectangles cache since zoom/target has changed
        self.rebuild_word_widget_rects(target)

    def paintEvent(self, event):
        """Renders the PDF page, selection, and border."""
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#1e1e1e")) # Dark background

        if self._page:
            # Calculate the target rectangle for the page image (maintaining aspect ratio)
            target = self.image_draw_rect()
            
            # Check if the cached pixmap is invalid (e.g., due to resize)
            if self._pixmap is None or abs(self._pixmap.width() / max(1.0, self.devicePixelRatioF()) - target.width()) > 1 or abs(self._pixmap.height() / max(1.0, self.devicePixelRatioF()) - target.height()) > 1:
                # Re-render the pixmap if needed
                self.ensure_pixmap_for_target(target)

            if self._pixmap:
                # Draw the rendered PDF page
                painter.drawPixmap(target.topLeft(), self._pixmap)

        # draw selection
        if self._sel_word_rects:
            pen = QPen(QColor(255, 255, 255, 220)) # Light border for selection
            pen.setWidth(1)
            painter.setPen(pen)
            for wr in self._sel_word_rects:
                painter.fillRect(wr, QColor(0, 120, 215, 80)) # Blue selection box
                painter.drawRect(wr) # Border for selection

        # draw page border based on tag
        if hasattr(self, "border_color") and self.border_color:
            pen = QPen(QColor(self.border_color))
            pen.setWidth(6) # Thick border
            pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRect(self.image_draw_rect()) # Draw colored border

        painter.end()


    def mousePressEvent(self, event):
        """Starts a selection drag operation."""
        if event.button() == Qt.MouseButton.LeftButton and self._page:
            target = self.image_draw_rect()
            if target.contains(event.position().toPoint()): # Only start selection inside page
                self._selecting = True
                p = event.position().toPoint()
                self._selection = QRect(p, p) # Initialize selection rect
                self.selectionChanged.emit()
                self.update()

    def mouseMoveEvent(self, event):
        """Handles mouse movement for selection and cursor changes."""
        p = event.position().toPoint()
        hovered_word = False
        if self._page:
            target = self.image_draw_rect()
            # Check if mouse is over a word to change cursor to IBeam
            if target.contains(p) and self._word_rects_widget:
                for wr in self._word_rects_widget:
                    if wr.contains(p):
                        hovered_word = True
                        break
        self.setCursor(Qt.CursorShape.IBeamCursor if hovered_word else Qt.CursorShape.ArrowCursor)

        # If actively selecting, update the selection rectangle
        if self._selecting and self._selection:
            self._selection.setBottomRight(p)
            self.compute_word_selection() # Update selected words live
            self.update() # Repaint

    def mouseReleaseEvent(self, event):
        """Stops the selection drag operation."""
        if event.button() == Qt.MouseButton.LeftButton and self._selecting:
            self._selecting = False
            # If selection is tiny (a click), clear it
            if self._selection and self._selection.width() < 3 and self._selection.height() < 3:
                self._selection = None
                self._sel_word_rects = []
            
            self.compute_word_selection() # Finalize selected words
            self.selectionChanged.emit()
            self.update()

    def widget_rect_to_page_rect(self, widget_rect: QRect):
        """
        Converts a QRect from widget coordinates to a fitz.Rect
        in PDF page coordinates (points).
        """
        if not self._page:
            return None
        target = self.image_draw_rect() # Get the page draw area
        inter = widget_rect.intersected(target) # Find intersection
        if inter.isEmpty():
            return None
        
        # Convert coordinates from widget space to page space
        x0 = (inter.left() - target.left()) * 1.0 / self._zoom
        y0 = (inter.top() - target.top()) * 1.0 / self._zoom
        x1 = (inter.right() - target.left()) * 1.0 / self._zoom
        y1 = (inter.bottom() - target.top()) * 1.0 / self._zoom
        return fitz.Rect(x0, y0, x1, y1)

    def page_rect_to_widget_rect(self, rect_pts: fitz.Rect) -> QRect:
        """
        Converts a fitz.Rect (in PDF page coordinates) to a QRect
        in widget coordinates.
        """
        target = self.image_draw_rect()
        
        # Convert coordinates from page space to widget space
        x0 = int(target.left() + rect_pts.x0 * self._zoom)
        y0 = int(target.top() + rect_pts.y0 * self._zoom)
        x1 = int(target.left() + rect_pts.x1 * self._zoom)
        y1 = int(target.top() + rect_pts.y1 * self._zoom)
        return QRect(x0, y0, x1 - x0, y1 - y0)

    def compute_word_selection(self):
        """
        Updates `self._sel_word_rects` based on the current
        `self._selection` rectangle.
        """
        self._sel_word_rects = []
        if not self._page or not self.has_selection():
            return
        
        # Convert widget selection rect to page coordinates
        sel_rect_page = self.widget_rect_to_page_rect(self._selection.normalized())
        if not sel_rect_page:
            return
        
        # Find all words that intersect the selection rectangle
        wrs = []
        for w in self._words:
            x0, y0, x1, y1, *_ = w
            wr = fitz.Rect(x0, y0, x1, y1)
            if wr.intersects(sel_rect_page):
                # If intersecting, add its *widget* rect to the list
                wrs.append(self.page_rect_to_widget_rect(wr))
        self._sel_word_rects = wrs

    def selected_text(self):
        """
        Returns the text currently selected by the user.
        """
        if not self._page or not self.has_selection():
            return ""
        
        # Convert widget selection rect to page coordinates
        sel_rect_page = self.widget_rect_to_page_rect(self._selection.normalized())
        if not sel_rect_page:
            return ""
        
        # Find all words intersecting the selection
        words = []
        for w in self._words:
            x0, y0, x1, y1, text, *_ = w
            if fitz.Rect(x0, y0, x1, y1).intersects(sel_rect_page):
                words.append((y0, x0, text)) # Store with position for sorting
        
        # Sort words by vertical, then horizontal position
        words.sort()
        
        # Reconstruct the text, adding line breaks
        out = []
        last_y = None
        for y, x, t in words:
            if last_y is not None and abs(y - last_y) > 5: # Simple line break detection
                out.append("\n")
            out.append(t)
            last_y = y
        
        return " ".join(out).replace(" \n ", "\n").strip() # Clean up newlines

    def resizeEvent(self, event):
        """Handles widget resize, invalidating the pixmap cache."""
        super().resizeEvent(event)
        self._pixmap = None # Pixmap must be re-rendered
        self.update()

        
class TimelineStrip(QWidget):
    """
    A widget displayed at the bottom of the window, showing a
    colored bar for all pages in the PDF, allowing quick navigation.
    """
    pageClicked = pyqtSignal(int) # Emitted when a page segment is clicked

    def __init__(self, parent=None):
        """Initializes the timeline widget."""
        super().__init__(parent)
        self.setMinimumHeight(22)
        self.setMaximumHeight(28)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.total_pages = 0
        self.page_tags = {} # {page_index: "tag_color"}
        self.current_file_page = 0 # The currently viewed page index
        self.bg_color = QColor("#1E1E1E")

    def mousePressEvent(self, event):
        """Handles clicking on the timeline to navigate."""
        if self.total_pages <= 0:
            return
        x = int(event.position().x())
        w = max(1, self.width())
        # Calculate which page index corresponds to the click position
        target = int((x / w) * self.total_pages)
        target = min(max(0, target), self.total_pages - 1) # Clamp to valid range
        self.pageClicked.emit(target)

    def mouseMoveEvent(self, event):
        """Shows a tooltip with page number and tag on hover."""
        if self.total_pages <= 0:
            return
        x = int(event.position().x())
        w = max(1, self.width())
        # Calculate page index under cursor
        idx = int((x / w) * self.total_pages)
        idx = min(max(0, idx), self.total_pages - 1)
        tag = self.page_tags.get(idx, "none")
        self.setToolTip(f"Page {idx + 1}  Tag {tag}")

    def set_total_pages(self, n):
        """Sets the total number of pages to display."""
        self.total_pages = max(0, int(n))
        self.update()

    def set_page_tags(self, tags_dict):
        """Sets the dictionary of page tags."""
        self.page_tags = dict(tags_dict) if tags_dict else {}
        self.update()

    def set_current_file_page(self, page_index):
        """Sets the currently viewed page index to highlight it."""
        self.current_file_page = max(0, int(page_index))
        self.update()

    def sizeHint(self):
        """Provides a default size hint."""
        return QSize(200, 24)

    def paintEvent(self, event):
        """Paints the timeline bar with colored segments for each page tag."""
        if self.total_pages <= 0:
            return

        painter = QPainter(self)
        painter.fillRect(self.rect(), self.bg_color)

        w = self.width()
        h = self.height()

        # Draw a colored block for each page
        for i in range(self.total_pages):
            x0 = int(i * w / self.total_pages)
            x1 = int((i + 1) * w / self.total_pages)
            strip_w = max(1, x1 - x0) # Width of this page's segment

            tag = self.page_tags.get(i, "none")
            if tag == "none":
                col = QColor("#000000") # Use black for untagged
            else:
                col = QColor(TAG_COLORS.get(tag, TAG_COLORS["none"]))

            painter.fillRect(x0, 0, strip_w, h, col)

        # Highlight current file page
        cur_x0 = int(self.current_file_page * w / self.total_pages)
        cur_x1 = int((self.current_file_page + 1) * w / self.total_pages)
        cur_w = max(2, cur_x1 - cur_x0) # Width of the highlight

        # Draw a white box outline for the current page
        outer_pen = QPen(QColor("#000000"))
        outer_pen.setWidth(1)
        outer_pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        painter.setPen(outer_pen)
        painter.drawLine(cur_x0 - 1, 0, cur_x0 - 1, h - 1)
        painter.drawLine(cur_x0 + cur_w, 0, cur_x0 + cur_w, h - 1)

        # Draw white inner indicator
        inner_pen = QPen(QColor("#FFFFFF"))
        inner_pen.setWidth(1)
        inner_pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(inner_pen)
        painter.drawRect(cur_x0, 1, cur_w - 1, h - 2)

        painter.end()

class PDFTaggerApp(QMainWindow):
    """
    The main application window, orchestrating the toolbar, sidebar,
    PDF view, and tag management.
    """
    def __init__(self, pdf_path=None):
        """Initializes the main application window and UI components."""
        super().__init__()
        self.setWindowTitle("PDF Study Tagger")
        self.setGeometry(100, 100, 1200, 800)
        self.doc = None         # The fitz.Document object
        self.pdf_path = pdf_path # Filesystem path to the PDF
        self.tags_path = ""      # Filesystem path to the .json tags file
        self.page_tags = {}      # Dictionary mapping {page_index: "tag_color"}
        
        # Core App State
        self.current_page_index = 0 # Index within the *visible_pages* list
        self.total_pages = 0        # Total pages in the PDF document
        self.visible_pages = []     # List of page indices matching the current filter
        self.active_filters = set() # Set of tags to show (e.g., {"green", "red"})
        self.search_hits = []       # List of (page_num, fitz.Rect) for search results
        self.current_hit_index = -1 # Current index in self.search_hits

        # --- Main Layout ---
        self.main_layout = QHBoxLayout()
        self.central_widget = QWidget()
        self.central_widget.setLayout(self.main_layout)
        self.setCentralWidget(self.central_widget)

        # --- Sidebar (Thumbnail List) ---
        self.thumbnail_list_widget = QListWidget()
        self.setup_sidebar()
        self.main_layout.addWidget(self.thumbnail_list_widget)
        self.thumb_title_labels = {} # Cache for page number labels in the sidebar

        # --- Main Content Area (PDF Viewer) ---
        self.main_content_widget = QWidget()
        self.main_content_layout = QVBoxLayout()
        self.main_content_layout.setContentsMargins(0, 0, 0, 0)
        self.main_content_widget.setLayout(self.main_content_layout)
        
        self.pdf_frame = QFrame()
        self.pdf_frame.setStyleSheet("background-color: #1e1e1e; padding: 10px;")
        frame_layout = QVBoxLayout(self.pdf_frame)
        frame_layout.setContentsMargins(10, 10, 10, 10)
        frame_layout.addWidget(PDFPageView(self)) # Add the custom PDF view
        self.pdf_viewer_label = self.pdf_frame.findChild(PDFPageView)
        self.pdf_viewer_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.pdf_viewer_label.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.main_content_layout.addWidget(self.pdf_frame, 1) # PDF view takes up most space
        
        # --- Status Bar ---
        self.status_bar_layout = QHBoxLayout()
        
        self.page_label = QLabel("Page 0 / 0") # Page x / y
        self.tag_counts_label = QLabel("") # Tag summary
        self.tag_counts_label.setStyleSheet("padding: 5px; color: #AAA;")

        self.status_bar_layout.addWidget(self.page_label)
        self.status_bar_layout.addWidget(self.tag_counts_label)
        self.status_bar_layout.addStretch()

        # --- Filter Checkboxes (in Status Bar) ---
        self.filters_container = QWidget()
        self.filters_layout = QHBoxLayout()
        self.filters_layout.setContentsMargins(0, 0, 0, 0)
        self.filters_layout.setSpacing(8)

        self.filters_label = QLabel("Filters")
        self.filters_label.setStyleSheet("padding: 5px; color: #CCC;")

        self.cb_green = QCheckBox("green")
        self.cb_yellow = QCheckBox("yellow")
        self.cb_red = QCheckBox("red")
        self.cb_none = QCheckBox("undefined")

        for cb in (self.cb_green, self.cb_yellow, self.cb_red, self.cb_none):
            cb.setChecked(True) # Start with all filters on
            cb.toggled.connect(self.on_filter_checkbox_changed)
            cb.setStyleSheet("color: #EEE;")

        self.filters_layout.addWidget(self.filters_label)
        self.filters_layout.addWidget(self.cb_green)
        self.filters_layout.addWidget(self.cb_yellow)
        self.filters_layout.addWidget(self.cb_red)
        self.filters_layout.addWidget(self.cb_none)
        self.filters_container.setLayout(self.filters_layout)

        self.status_bar_layout.addWidget(self.filters_container)
        
        status_frame = QFrame()
        status_frame.setLayout(self.status_bar_layout)
        status_frame.setStyleSheet("background-color: #2D2D2D; color: white;")
        self.main_content_layout.addWidget(status_frame)
        
        # --- Timeline Strip (Bottom) ---
        self.timeline_strip = TimelineStrip(self)
        self.timeline_strip.pageClicked.connect(self.on_timeline_clicked)
        self.main_content_layout.addWidget(self.timeline_strip)
        
        # Install event filter to catch global key presses (like arrow keys)
        QApplication.instance().installEventFilter(self)

        self.thumbnail_list_widget.setMinimumWidth(260)
        self.thumbnail_list_widget.setMaximumWidth(400)

        # --- Splitter (Sidebar <-> Main Content) ---
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.addWidget(self.thumbnail_list_widget)
        self.splitter.addWidget(self.main_content_widget)
        
        self.splitter.setSizes([260, 1000]) # Initial sizes
        self.splitter.setStretchFactor(0, 0) # Sidebar width is fixed
        self.splitter.setStretchFactor(1, 1) # Main content stretches

        self.main_layout.addWidget(self.splitter)

        # self.main_layout.addWidget(self.splitter) # Note: Original code had this line twice
        
        # --- Final Setup ---
        self.setup_toolbar()
        self.setup_shortcuts()
        
        # Load PDF
        if pdf_path:
            self.load_pdf(pdf_path)
        else:
            self.open_file() # Show file dialog if no path provided
            
        self.pdf_viewer_label.setFocus() # Set focus to PDF view for keyboard events
        self.showMaximized()

    def on_timeline_clicked(self, file_page_index):
        """
        Jumps to a page clicked on the bottom timeline strip.
        If the exact page is filtered out, jumps to the nearest visible page.
        """
        if not self.visible_pages:
            return
        
        # If the clicked page is visible, go to it
        if file_page_index in self.visible_pages:
            self.current_page_index = self.visible_pages.index(file_page_index)
            self.render_page()
            return
        
        # If clicked page is filtered out, find nearest visible page
        after = next((p for p in self.visible_pages if p >= file_page_index), None)
        before = next((p for p in reversed(self.visible_pages) if p <= file_page_index), None)
        chosen = after if after is not None else before # Prefer 'after'
        if chosen is not None:
            self.current_page_index = self.visible_pages.index(chosen)
            self.render_page()

    def select_all_text_on_slide(self):
        """Select all text in the current page view."""
        if hasattr(self, "pdf_viewer_label") and isinstance(self.pdf_viewer_label, PDFPageView):
            self.pdf_viewer_label.select_all_text()

    def _title_bg_css(self, tag):
        """Helper to generate CSS for sidebar page number labels."""
        fg = "#000" if tag in ("yellow",) else "#FFF" # Black text on yellow bg
        bg = TAG_COLORS.get(tag, TAG_COLORS["none"])
        return f"background-color: {bg}; color: {fg}; padding: 6px 10px; border-radius: 6px;"

    def generate_thumbnail_pixmap(self, page_num):
        """Renders a small thumbnail QPixmap for a given page number."""
        try:
            page = self.doc.load_page(page_num)
            thumb_mat = fitz.Matrix(0.2, 0.2) # Low-res zoom
            pix = page.get_pixmap(matrix=thumb_mat)
            img_format = QImage.Format.Format_RGB888 if pix.alpha == 0 else QImage.Format.Format_RGBA8888
            qimage = QImage(pix.samples, pix.width, pix.height, pix.stride, img_format)
            return QPixmap.fromImage(qimage)
        except Exception as e:
            print(f"Error generating thumbnail for page {page_num}: {e}")
            return QPixmap() # Return empty pixmap on error
        
    def center_sidebar_on_current(self):
        """Scrolls the sidebar list to center on the current page item."""
        if not self.visible_pages:
            return
        actual_page_num = self.visible_pages[self.current_page_index]
        item = self.thumbnail_list_widget.item(actual_page_num) # Get item by file index
        if item and not item.isHidden():
            self.thumbnail_list_widget.scrollToItem(
                item,
                QAbstractItemView.ScrollHint.PositionAtCenter
            )

    def build_thumbnail_row(self, page_num):
        """Creates the custom widget for a single row in the sidebar list."""
        row = QWidget()
        lay = QHBoxLayout(row)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(10)

        # Thumbnail image
        thumb = QLabel()
        thumb.setFixedSize(128, 96)
        thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pm = self.generate_thumbnail_pixmap(page_num)
        if not pm.isNull():
            thumb.setPixmap(pm.scaled(thumb.size(), Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))

        # Page number label
        title = QLabel(f"{page_num + 1}")
        title.setFixedWidth(60)
        title.setMaximumHeight(50)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tag = self.page_tags.get(page_num, "none")
        title.setStyleSheet(self._title_bg_css(tag))
        title.setMargin(6)

        lay.addWidget(thumb)
        lay.addWidget(title)


        # lay.addWidget(thumb) # Note: Original code had these two lines duplicated
        # lay.addWidget(title, 1)

        # Store label in cache for future updates
        self.thumb_title_labels[page_num] = title
        return row

    def eventFilter(self, obj, event):
        """
        Global event filter to capture key presses for navigation
        (Arrow Up/Down/Left/Right) outside of text inputs.
        """
        if event.type() == QEvent.Type.KeyPress:
            key = event.key()
            
            # If search input is focused, let it handle keys
            if self.search_input and self.search_input.hasFocus():
                return super().eventFilter(obj, event)
            
            # Page navigation
            if key == Qt.Key.Key_Right or key == Qt.Key.Key_Down:
                self.next_page()
                return True # Event handled
            if key == Qt.Key.Key_Left or key == Qt.Key.Key_Up:
                self.prev_page()
                return True # Event handled
        
        return super().eventFilter(obj, event) # Pass event on

    def setup_shortcuts(self):
        """Configures global keyboard shortcuts."""
        def make_shortcut(seq, fn):
            # Helper to create an application-wide shortcut
            sc = QShortcut(QKeySequence(seq), self)
            sc.setContext(Qt.ShortcutContext.ApplicationShortcut)
            sc.activated.connect(fn)
            return sc

        def tag_and_next(color):
            """Helper function for 1, 2, 3 keys: Tag, then advance."""
            before = self.visible_pages[self.current_page_index] if self.visible_pages else None
            self.apply_tag_for_current_context(color)

            # Special logic: only advance if the page *wasn't* filtered out by the tagging
            if color in {"green", "yellow", "red"}:
                after = self.visible_pages[self.current_page_index] if self.visible_pages else None

                if after == before: # If current page is still visible
                    self.next_page()
                    self.center_sidebar_on_current()
                else: # Current page was filtered out, new page is loaded
                    self.center_sidebar_on_current()

        # --- Register Shortcuts ---
        # Tagging shortcuts (1, 2, 3, 4)
        make_shortcut("1", lambda: tag_and_next("green"))
        make_shortcut("2", lambda: tag_and_next("yellow"))
        make_shortcut("3", lambda: tag_and_next("red"))
        make_shortcut("4", lambda: (self.apply_tag_for_current_context("none"), self.next_page(), self.center_sidebar_on_current()))

        # Copy shortcuts
        make_shortcut("C", lambda: self.copy_all_text_on_slide())
        make_shortcut("Shift+C", self.copy_current_slide_to_clipboard)
        make_shortcut(QKeySequence(QKeySequence.StandardKey.Copy), self.copy_selected_text) # Ctrl+C
        
        # Other standard shortcuts
        make_shortcut(QKeySequence(QKeySequence.StandardKey.Find), self.focus_search) # Ctrl+F
        make_shortcut(QKeySequence(QKeySequence.StandardKey.SelectAll), self.select_all_text_on_slide) # Ctrl+A

        # Save selection
        make_shortcut(QKeySequence(QKeySequence.StandardKey.Save), self.export_filtered_pages)  # Ctrl+S
        
    def copy_all_text_on_slide(self):
        """Copy all text from the current slide to the clipboard."""
        if not self.doc or not self.visible_pages:
            return
        actual_page_num = self.visible_pages[self.current_page_index]
        try:
            page = self.doc.load_page(actual_page_num)
            text = page.get_text("text") # Get full page text
            if text:
                QApplication.clipboard().setText(text.strip())
        except Exception as e:
            print(f"Copy text failed for page {actual_page_num}: {e}")


    def copy_current_slide_to_clipboard(self):
        """Renders the current page at high-res and copies it as an image."""
        if not self.doc or not self.visible_pages:
            return
        # Don't trigger if user is just typing 'C' in the search box
        if self.search_input and self.search_input.hasFocus():
            return
        
        actual_page_num = self.visible_pages[self.current_page_index]
        try:
            page = self.doc.load_page(actual_page_num)
            zoom = 2.5 # High-res zoom
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat)
            img_format = QImage.Format.Format_RGB888 if pix.alpha == 0 else QImage.Format.Format_RGBA8888
            qimage = QImage(pix.samples, pix.width, pix.height, pix.stride, img_format)
            pm = QPixmap.fromImage(qimage)
            QApplication.clipboard().setPixmap(pm) # Copy image to clipboard
        except Exception as e:
            print(f"Copy failed for page {actual_page_num}: {e}")



    def apply_tag_for_current_context(self, color):
        """
        Applies a tag. If sidebar items are selected, tags all
        selected items. Otherwise, tags the currently viewed page.
        """
        # Don't tag if user is typing in search
        if self.search_input.hasFocus():
            return

        # Context 1: Multiple items selected in sidebar
        if self.thumbnail_list_widget.selectedItems():
            page_numbers = [it.data(Qt.ItemDataRole.UserRole) for it in self.thumbnail_list_widget.selectedItems()]
            self.tag_multiple_pages(page_numbers, color)
        # Context 2: No selection, tag current page
        elif self.visible_pages:
            actual_page_num = self.visible_pages[self.current_page_index]
            self.tag_multiple_pages([actual_page_num], color)

    def setup_sidebar(self):
        """Initializes settings and styling for the thumbnail list widget."""
        self.thumbnail_list_widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.thumbnail_list_widget.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection) # Allow multi-select
        self.thumbnail_list_widget.setViewMode(QListWidget.ViewMode.ListMode)
        self.thumbnail_list_widget.setIconSize(QSize(128, 128))
        self.thumbnail_list_widget.setMovement(QListWidget.Movement.Static)
        self.thumbnail_list_widget.setSpacing(5)
        self.thumbnail_list_widget.itemSelectionChanged.connect(self.on_selection_changed)
        self.thumbnail_list_widget.setStyleSheet("""
            QListWidget {
                background-color: #2D2D2D;
                color: white;
                border: none;
            }
            QListWidget::item {
                border-radius: 5px;
                background-color: #444444;
            }
            QListWidget::item:selected {
                background-color: #6e6e6e;
            }
        """)

    def reset_search_state(self):
        """Clears all search results and resets search UI."""
        self.search_hits = []
        self.current_hit_index = -1
        if hasattr(self, "search_status_label"):
            self.search_status_label.setText("0 matches")

    def update_search_status(self):
        """Updates the 'x of y matches' label in the toolbar."""
        total = len(self.search_hits)
        if total == 0 or self.current_hit_index < 0:
            self.search_status_label.setText(f"{total} matches")
        else:
            self.search_status_label.setText(f"{self.current_hit_index + 1} of {total} matches")

    def setup_toolbar(self):
        """Creates and configures the main application toolbar."""
        toolbar = QToolBar("Main Toolbar")
        toolbar.setMovable(False)
        self.addToolBar(toolbar)

        # File/View actions
        toolbar.addAction("Open", self.open_file)
        toolbar.addAction("Sidebar", self.toggle_sidebar)
        toolbar.addSeparator()

        # Navigation actions
        toolbar.addAction("Prev", self.prev_page)
        toolbar.addAction("Next", self.next_page)
        toolbar.addSeparator()

        # Export action
        export_action = toolbar.addAction("Export selection", self.export_filtered_pages)
        toolbar.addSeparator()

        # Spacer to push search controls to the right
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        toolbar.addWidget(spacer)

        # Search controls (right-aligned)
        self.search_input = QLineEdit(self)
        self.search_input.setPlaceholderText("Search (Ctrl+F)")
        self.search_input.returnPressed.connect(self.run_search)
        self.search_input.setFixedWidth(200)
        toolbar.addWidget(self.search_input)

        self.search_prev_btn = QToolButton(self)
        self.search_prev_btn.setText("Prev")
        self.search_prev_btn.clicked.connect(self.on_search_prev_clicked)
        toolbar.addWidget(self.search_prev_btn)

        self.search_next_btn = QToolButton(self)
        self.search_next_btn.setText("Next")
        self.search_next_btn.clicked.connect(self.on_search_next_clicked)
        toolbar.addWidget(self.search_next_btn)

        self.search_status_label = QLabel("0 matches")
        self.search_status_label.setStyleSheet("padding-left: 6px; color: #AAA;")
        toolbar.addWidget(self.search_status_label)


    def focus_search(self):
        """Selects the search input text field."""
        self.search_input.setFocus()
        self.search_input.selectAll()

    def toggle_sidebar(self):
        """Shows or hides the thumbnail sidebar."""
        is_visible = self.thumbnail_list_widget.isVisible()
        self.thumbnail_list_widget.setVisible(not is_visible)

    def on_selection_changed(self):
        """
        Handles selection changes in the sidebar.
        If a single item is clicked, navigates to that page.
        """
        self.thumbnail_list_widget.setFocus()
        current_item = self.thumbnail_list_widget.currentItem()
        if not current_item:
            return
        
        page_num = current_item.data(Qt.ItemDataRole.UserRole)
        
        # Navigate if the clicked page is visible and not already active
        if page_num in self.visible_pages:
            if self.visible_pages[self.current_page_index] != page_num:
                self.current_page_index = self.visible_pages.index(page_num)
                self.render_page()

    def export_filtered_pages(self):
        """Exports currently visible (filtered) pages to a single PDF."""
        if not self.doc or not self.visible_pages or not self.pdf_path:
            return

        base_name = os.path.splitext(os.path.basename(self.pdf_path))[0]
        default_name = f"{base_name}_filtered.pdf"

        save_path, _ = QFileDialog.getSaveFileName(
            self,
            "Save filtered PDF as",
            os.path.join(os.path.dirname(self.pdf_path), default_name),
            "PDF Files (*.pdf)"
        )
        if not save_path:
            return

        try:
            new_pdf = fitz.open()

            # Sort visible pages and copy in as few ranges as possible
            visible_sorted = sorted(self.visible_pages)
            start = visible_sorted[0]
            end = start
            for idx in range(1, len(visible_sorted)):
                if visible_sorted[idx] == end + 1:
                    end = visible_sorted[idx]
                else:
                    new_pdf.insert_pdf(self.doc, from_page=start, to_page=end)
                    start = end = visible_sorted[idx]
            new_pdf.insert_pdf(self.doc, from_page=start, to_page=end)

            new_pdf.save(save_path, deflate=True)  # deflate compresses streams
            new_pdf.close()
            print(f"Filtered PDF saved to {save_path}")
        except Exception as e:
            print(f"Error exporting filtered PDF: {e}")

    def open_file(self):
        """Opens a QFileDialog to select a new PDF file."""
        path, _ = QFileDialog.getOpenFileName(self, "Open PDF File", "", "PDF Files (*.pdf)")
        if path:
            self.load_pdf(path)

    def load_pdf(self, pdf_path):
        """Loads a PDF document, its tags, and populates the UI."""
        if self.doc:
            self.doc.close() # Close any previously open document
        try:
            self.doc = fitz.open(pdf_path)
            self.pdf_path = pdf_path
            self.total_pages = len(self.doc)
            self.setWindowTitle(f"PDF Study Tagger - {os.path.basename(pdf_path)}")

            # Define the path for the companion tag file
            base_filename = os.path.splitext(self.pdf_path)[0]
            self.tags_path = f"{base_filename}_pdf-tagger-sav.json"

            # Load tags if the file exists
            if os.path.exists(self.tags_path):
                with open(self.tags_path, 'r') as f:
                    loaded = json.load(f)
                    # Ensure keys are integers
                    self.page_tags = {int(k): v for k, v in loaded.items()}
            else:
                self.page_tags = {}

            self.ensure_all_pages_in_tags() # Make sure all pages have at least a "none" tag
            self.save_tags() # Save to create file or clean up old entries

            # --- Update UI ---
            self.populate_sidebar()
            self.current_page_index = 0
            self.update_tag_counts_label()
            self.reset_search_state()
            self.on_filter_checkbox_changed() # Apply default filters (which triggers render)
            
            # Update the bottom timeline
            if hasattr(self, "timeline_strip"):
                self.timeline_strip.set_total_pages(self.total_pages)
                self.timeline_strip.set_page_tags(self.page_tags)
                self.timeline_strip.set_current_file_page(self.visible_pages[self.current_page_index] if self.visible_pages else 0)

        except Exception as e:
            print(f"Error loading PDF: {e}")
            self.page_label.setText("Error loading PDF")
            self.reset_search_state()

    def populate_sidebar(self):
        """Clears and rebuilds the entire thumbnail list from scratch."""
        self.thumbnail_list_widget.clear()
        self.thumb_title_labels.clear()

        for i in range(self.total_pages):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, i) # Store page index
            item.setForeground(QColor("white"))
            item.setSizeHint(QSize(200, 110))
            self.thumbnail_list_widget.addItem(item)
            # Set the custom widget for the item
            self.thumbnail_list_widget.setItemWidget(item, self.build_thumbnail_row(i))

        self.update_sidebar_filter_view()

    def generate_thumbnail_icon(self, page_num):
        """
        Generates a QIcon for a page (with a colored dot for the tag).
        Note: This function is not currently used, but was present.
        """
        try:
            page = self.doc.load_page(page_num)
            thumb_mat = fitz.Matrix(0.2, 0.2)
            pix = page.get_pixmap(matrix=thumb_mat)
            img_format = QImage.Format.Format_RGB888 if pix.alpha == 0 else QImage.Format.Format_RGBA8888
            qimage = QImage(pix.samples, pix.width, pix.height, pix.stride, img_format)
            pixmap = QPixmap.fromImage(qimage)
            tag = self.page_tags.get(page_num, "none")
            if tag != "none":
                # Paint a colored dot on the thumbnail
                painter = QPainter(pixmap)
                color = QColor(TAG_COLORS[tag])
                painter.setBrush(color)
                painter.setPen(Qt.PenStyle.NoPen)
                dot_size = int(pixmap.width() * 0.15)
                margin = int(pixmap.width() * 0.05)
                painter.drawEllipse(pixmap.width() - dot_size - margin, margin, dot_size, dot_size) 
                painter.end()
            return QIcon(pixmap)
        except Exception as e:
            print(f"Error generating thumbnail for page {page_num}: {e}")
            return QIcon()

    def tag_multiple_pages(self, page_numbers, color):
        """Applies a tag to a list of page numbers and updates the UI."""
        if not self.doc:
            return

        current_visible_page = self.visible_pages[self.current_page_index] if self.visible_pages else -1
        did_tag_current = False

        for page_num in page_numbers:
            if color == "none":
                if page_num in self.page_tags:
                    self.page_tags[page_num] = "none" # Set to none, don't delete
            else:
                self.page_tags[page_num] = color

            if page_num == current_visible_page:
                did_tag_current = True

            # Update the sidebar label style
            title_lbl = self.thumb_title_labels.get(page_num)
            if title_lbl:
                tag = self.page_tags.get(page_num, "none")
                title_lbl.setStyleSheet(self._title_bg_css(tag))

        # Update the main viewer border if the current page was tagged
        if did_tag_current:
            self.update_page_border()

        self.save_tags()
        self.update_tag_counts_label()
        self.on_filter_checkbox_changed() # Re-apply filters
        self.timeline_strip.set_page_tags(self.page_tags) # Update bottom bar
        
    def update_page_border(self):
        """Updates the colored border around the main PDF view based on its tag."""
        if not self.visible_pages:
            if hasattr(self.pdf_viewer_label, "border_color"):
                self.pdf_viewer_label.border_color = None
                self.pdf_viewer_label.update()
            return

        actual_page_num = self.visible_pages[self.current_page_index]
        tag = self.page_tags.get(actual_page_num, "none")

        if tag == "none":
            color = None
        else:
            color = TAG_COLORS.get(tag, "#333333")

        self.pdf_viewer_label.border_color = color
        self.pdf_viewer_label.update() # Trigger repaint of PDF view

    def save_tags(self):
        """Saves the current `self.page_tags` dictionary to its JSON file."""
        try:
            # Sort keys for clean JSON
            ordered = {str(k): self.page_tags[k] for k in sorted(self.page_tags.keys())}
            with open(self.tags_path, 'w') as f:
                json.dump(ordered, f, indent=2)
        except Exception as e:
            print(f"Error saving tags: {e}")

    def on_filter_checkbox_changed(self):
        """Called when any filter checkbox is toggled."""
        selected = set()
        if self.cb_green.isChecked():
            selected.add("green")
        if self.cb_yellow.isChecked():
            selected.add("yellow")
        if self.cb_red.isChecked():
            selected.add("red")
        if self.cb_none.isChecked():
            selected.add("none")

        # If all 4 are selected, it's the same as no filter
        if len(selected) == 4:
            self.active_filters = set()
        else:
            self.active_filters = selected

        self.update_filter_view()

        # Force search refresh when filters change
        if self.search_input.text().strip():
            # Reset last search to ensure re-search even with same text
            self._last_search_text = None
            self.run_search()
        else:
            self.reset_search_state()

        if self.visible_pages:
            self.center_sidebar_on_current()


    def update_tag_counts_label(self):
        """Updates the status bar label with counts and percentages of tags."""
        if self.total_pages == 0:
            self.tag_counts_label.setText("")
            return

        counts = {"green": 0, "yellow": 0, "red": 0}
        for tag in self.page_tags.values():
            if tag in counts:
                counts[tag] += 1
        
        count_strings = []
        for color in ["green", "yellow", "red"]:
            count = counts[color]
            if count > 0:
                percent = (count / self.total_pages) * 100
                hex_color = TAG_COLORS[color]
                # Use rich text for colored dots
                count_strings.append(
                    f'<span style="color: {hex_color}; font-size: 18px;">●</span> '
                    f"{count} ({percent:.0f}%)"
                )
        
        self.tag_counts_label.setText(" | ".join(count_strings))

    def ensure_all_pages_in_tags(self):
        """
        Ensures `self.page_tags` is valid.
        1. Removes tags for pages that don't exist (e.g., if PDF changed).
        2. Adds a "none" tag for any page that doesn't have a tag.
        """
        # 1. Prune invalid page numbers
        self.page_tags = {int(k): v for k, v in self.page_tags.items() if 0 <= int(k) < self.total_pages}
        # 2. Add "none" for missing pages
        for i in range(self.total_pages):
            if i not in self.page_tags:
                self.page_tags[i] = "none"

    def update_filter_view(self):
        """
        Updates the `self.visible_pages` list based on active filters
        and refreshes the UI.
        """
        if not self.doc:
            return

        # Get the *file page number* of the currently viewed page
        old_actual = self.visible_pages[self.current_page_index] if self.visible_pages else None

        # --- Update visible_pages list ---
        if not self.active_filters: # If no filters, show all
            self.visible_pages = list(range(self.total_pages))
        else:
            # Build list of pages that match the active filters
            self.visible_pages = [
                i for i in range(self.total_pages)
                if self.page_tags.get(i, "none") in self.active_filters
            ]

        self.update_sidebar_filter_view() # Hide/show items in sidebar

        # --- Handle navigation after filtering ---
        if not self.visible_pages:
            # No pages match, show empty view
            self.pdf_viewer_label.clear()
            self.pdf_viewer_label.setStyleSheet("border: none;")
            self.pdf_viewer_label.set_page(None) # Clear page from viewer
            self.page_label.setText("0 / 0 No pages match filter")
            self.current_page_index = 0
            return

        # Try to stay on the same page
        if old_actual in self.visible_pages:
            self.current_page_index = self.visible_pages.index(old_actual)
        else:
            # Current page was filtered out, find the nearest neighbor
            next_after = next((p for p in self.visible_pages if p > (old_actual if old_actual is not None else -1)), None)
            if next_after is not None:
                self.current_page_index = self.visible_pages.index(next_after)
            else:
                prev_before = next((p for p in reversed(self.visible_pages) if p < (old_actual if old_actual is not None else 0)), None)
                if prev_before is not None:
                    self.current_page_index = self.visible_pages.index(prev_before)
                else:
                    self.current_page_index = 0 # Default to first page
            self.reset_search_state() # Search is invalid after filter change

        self.render_page()
        self.center_sidebar_on_current()

    def on_search_next_clicked(self):
        """Handle clicking Next: perform search if needed, else go to next hit."""
        text = self.search_input.text().strip()
        if not text:
            return
        if not self.search_hits:  # No search yet
            self.run_search()
        else:
            self.search_next()

    def on_search_prev_clicked(self):
        """Handle clicking Prev: perform search if needed, else go to previous hit."""
        text = self.search_input.text().strip()
        if not text:
            return
        if not self.search_hits:  # No search yet
            self.run_search()
        else:
            self.search_prev()


    def update_sidebar_filter_view(self):
        """Hides or shows items in the sidebar list based on `self.visible_pages`."""
        is_filtered = bool(self.active_filters)
        for i in range(self.total_pages):
            item = self.thumbnail_list_widget.item(i)
            if is_filtered and i not in self.visible_pages:
                item.setHidden(True)
            else:
                item.setHidden(False)
                
    def render_page(self):
        """
        Loads and displays the page specified by `self.current_page_index`
        (which is an index into `self.visible_pages`).
        """
        if not self.doc or not self.visible_pages:
            return

        # Get the *actual file page number*
        actual_page_num = self.visible_pages[self.current_page_index]
        try:
            page = self.doc.load_page(actual_page_num)

            # Set the page in the viewer
            self.pdf_viewer_label.set_page(page)

            # Update status label
            self.page_label.setText(
                f"Page: {self.current_page_index + 1} / {len(self.visible_pages)} "
                f"(File Page: {actual_page_num + 1} / {self.total_pages})"
            )
            # Update border color
            self.update_page_border()

            # Update sidebar selection
            self.thumbnail_list_widget.blockSignals(True) # Avoid re-triggering navigation
            self.thumbnail_list_widget.setCurrentRow(actual_page_num)
            self.thumbnail_list_widget.blockSignals(False)
            
            # Update timeline highlight
            self.timeline_strip.set_current_file_page(actual_page_num)
        except Exception as e:
            print(f"Error rendering page {actual_page_num}: {e}")
            
        # self.timeline_strip.set_current_file_page(actual_page_num) # Note: Duplicated
        if hasattr(self, "timeline_strip"):
            self.timeline_strip.set_current_file_page(actual_page_num)

    def copy_selected_text(self):
        """Copies the currently selected text from the PDF view to the clipboard."""
        if hasattr(self, "pdf_viewer_label") and isinstance(self.pdf_viewer_label, PDFPageView):
            txt = self.pdf_viewer_label.selected_text()
            if txt:
                QApplication.clipboard().setText(txt)
            else:
                print("No text selection")

    def next_page(self):
        """Navigates to the next page in the `visible_pages` list."""
        if self.current_page_index < len(self.visible_pages) - 1:
            self.current_page_index += 1
            self.render_page()
            self.center_sidebar_on_current()

    def prev_page(self):
        """Navigates to the previous page in the `visible_pages` list."""
        if self.current_page_index > 0:
            self.current_page_index -= 1
            self.render_page()
            self.center_sidebar_on_current()
            
    def run_search(self):
        """
        Executes a search when Enter is pressed.
        If the same query is entered again, cycles through the found hits.
        """
        text = self.search_input.text().strip()
        if not text or not self.doc:
            self.reset_search_state()
            return

        # If same query as last time and hits already exist, just move to next
        if hasattr(self, "_last_search_text") and self._last_search_text == text and self.search_hits:
            self.current_hit_index = (self.current_hit_index + 1) % len(self.search_hits)
            self.go_to_hit(self.current_hit_index)
            self.update_search_status()
            return

        # New search query — find all matches
        self._last_search_text = text
        hits = []
        for page_num in self.visible_pages:
            page = self.doc.load_page(page_num)
            rects = page.search_for(text)
            for r in rects:
                hits.append((page_num, r))

        self.search_hits = hits
        if not hits:
            self.current_hit_index = -1
            self.update_search_status()
            return

        self.current_hit_index = 0
        self.go_to_hit(self.current_hit_index)
        self.update_search_status()


    def resizeEvent(self, event):
        """Handles window resize. Re-renders the page."""
        super().resizeEvent(event)
        self.render_page() # Re-render to fit new size

    def go_to_hit(self, idx):
        """Navigates to a specific search hit by its index."""
        if not self.search_hits:
            return
        
        idx = max(0, min(idx, len(self.search_hits) - 1))
        page_num, _ = self.search_hits[idx]
        
        # Navigate to the page of the hit
        if page_num in self.visible_pages:
            self.current_page_index = self.visible_pages.index(page_num)
            self.render_page()
            self.center_sidebar_on_current()

    def search_next(self):
        """Goes to the next search hit in the list (wraps around)."""
        if not self.search_hits:
            return
        self.current_hit_index = (self.current_hit_index + 1) % len(self.search_hits)
        self.go_to_hit(self.current_hit_index)
        self.update_search_status()

    def search_prev(self):
        """Goes to the previous search hit in the list (wraps around)."""
        if not self.search_hits:
            return
        self.current_hit_index = (self.current_hit_index - 1) % len(self.search_hits)
        self.go_to_hit(self.current_hit_index)
        self.update_search_status()

def force_dark_mode(app):
    """Apply a consistent dark mode palette and basic dark stylesheet."""
    dark_palette = QPalette()

    dark_palette.setColor(QPalette.ColorRole.Window, QColor(30, 30, 30))
    dark_palette.setColor(QPalette.ColorRole.WindowText, QColor(220, 220, 220))
    dark_palette.setColor(QPalette.ColorRole.Base, QColor(25, 25, 25))
    dark_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(45, 45, 45))
    dark_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(50, 50, 50))
    dark_palette.setColor(QPalette.ColorRole.ToolTipText, QColor(220, 220, 220))
    dark_palette.setColor(QPalette.ColorRole.Text, QColor(220, 220, 220))
    dark_palette.setColor(QPalette.ColorRole.Button, QColor(45, 45, 45))
    dark_palette.setColor(QPalette.ColorRole.ButtonText, QColor(220, 220, 220))
    dark_palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
    dark_palette.setColor(QPalette.ColorRole.Highlight, QColor(0, 120, 215))
    dark_palette.setColor(QPalette.ColorRole.HighlightedText, QColor(0, 0, 0))

    app.setPalette(dark_palette)

    app.setStyleSheet("""
        QToolTip {
            color: #ddd;
            background-color: #2a2a2a;
            border: 1px solid #444;
        }
        QLineEdit, QPlainTextEdit, QTextEdit {
            background-color: #202020;
            color: #ddd;
            border: 1px solid #555;
        }
        QPushButton {
            background-color: #3a3a3a;
            color: #ddd;
            border: 1px solid #555;
            padding: 4px 10px;
            border-radius: 4px;
        }
        QPushButton:hover {
            background-color: #4a4a4a;
        }
        QCheckBox, QLabel {
            color: #ddd;
        }
        QToolBar {
            background-color: #2b2b2b;
            spacing: 6px;
            padding: 4px;
        }
    """)

if __name__ == "__main__":
    # Main application entry point
    app = QApplication(sys.argv)
    force_dark_mode(app)
    
    pdf_path = None
    # Allow passing PDF path as a command-line argument
    if len(sys.argv) > 1:
        pdf_path = sys.argv[1]
        
    window = PDFTaggerApp(pdf_path)
    window.show()
    sys.exit(app.exec())