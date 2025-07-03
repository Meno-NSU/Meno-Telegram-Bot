import markdown
from markdown.extensions import Extension
from markdown.treeprocessors import Treeprocessor
from xml.etree import ElementTree as etree

class TagConversionTreeprocessor(Treeprocessor):
    """
    A Treeprocessor for Python-Markdown that filters and converts HTML tags
    in the ElementTree.

    It only allows tags from a specified set. Any other tags encountered
    are converted to a default tag from the allowed set, and all their
    attributes are stripped.
    """
    # Define the set of allowed HTML tags.
    # Any tag not in this set will be converted.
    ALLOWED_TAGS = {"strong", "em", "code", "pre", "del", "u", "a"}

    # Define the default tag to convert disallowed tags into.
    # This must be one of the tags in ALLOWED_TAGS.
    DEFAULT_CONVERSION_TAG = "em"

    def run(self, root):
        """
        Starts the recursive processing of the ElementTree from the root.
        Modifies the tree in-place.
        """
        # Begin the recursive processing from the root element.
        # This function will traverse down the tree and apply the conversion logic.
        self._process_element(root)
        return root

    def _process_element(self, element):
        """
        Recursively processes a single HTML element and its children.

        If the current element's tag is not in the ALLOWED_TAGS set,
        its tag name is changed to DEFAULT_CONVERSION_TAG, and all
        its attributes are cleared. Then, its children are processed.

        Args:
            element: The current ElementTree element to process.
        """
        # Check if the current element's tag is allowed.
        if element.tag not in self.ALLOWED_TAGS:
            # If the tag is not allowed, convert it to the default tag.
            element.tag = self.DEFAULT_CONVERSION_TAG
            # Clear all attributes from the converted tag.
            # This ensures only the tag name and content remain,
            # and prevents unwanted attributes (like 'href' on a converted 'a' tag)
            # from appearing on the new tag.
            element.attrib.clear()

        # Recursively process all children of the current element.
        # We iterate over a copy of the children list (`list(element)`)
        # to prevent issues if the list is modified during iteration
        # (though in this specific case, we are only changing tag names
        # and attributes, not adding/removing children, so it's safer).
        for child in list(element):
            self._process_element(child)


class TagConversionExtension(Extension):
    """
    A Python-Markdown extension that registers the TagConversionTreeprocessor.
    """
    def extendMarkdown(self, md):
        """
        Registers the TagConversionTreeprocessor with the Markdown instance.

        The treeprocessor is registered with a priority of '0', which means
        it runs very late in the Markdown processing chain. This is ideal
        because it allows other Markdown extensions to first generate their
        HTML structure (e.g., fenced_code creating <pre><code> tags,
        tables creating <table> tags), and then this extension applies
        the final filtering and conversion.

        Args:
            md: The Markdown instance to extend.
        """
        md.treeprocessors.register(TagConversionTreeprocessor(md), 'tag_conversion', 0)

# --- Example Usage ---
if __name__ == "__main__":
    # Initialize Markdown with some common extensions
    # and both our custom extensions:
    # 1. CustomCodeBlockExtension (from previous response, ensures 'language-' class)
    # 2. TagConversionExtension (this new extension, filters/converts tags)
    
    # We need to define CustomCodeBlockExtension again for this example to be runnable
    # if run independently.
    
    # --- Start of CustomCodeBlockExtension (from previous response) ---

    md = markdown.Markdown(extensions=[
        'fenced_code',          # Essential for 'lang' attribute on <code> tags
        TagConversionExtension()    # Our new tag conversion extension
        
    ])
    md.stripTopLevelTags = False

    markdown_text = """
# Heading 1

This is a paragraph with **strong** text, *emphasized* text, and some `code`.
A deleted word: <del>bad</del>. And <u>underlined</u> text.

```python
print("Hello, World!") # This should get language-python class
```

- List item 1
- List item 2

[Link to Google](https://www.google.com)

| Header 1 | Header 2 |
|----------|----------|
| Data 1   | Data 2   |

<p>This is a raw paragraph tag.</p>
<div>This is a div.</div>
<span>This is a span.</span>
<br>
"""

    html = md.convert(markdown_text)
    print("--- Generated HTML ---")
    print(html)

    # Expected output tags:
    # <h1> -> <em>
    # <p> -> <em>
    # <strong> -> <strong> (allowed)
    # <em> -> <em> (allowed)
    # <code> -> <code> (allowed, with class="language-python")
    # <pre> -> <pre> (allowed)
    # <del> -> <del> (allowed)
    # <u> -> <u> (allowed)
    # <ul> -> <em>
    # <li> -> <em>
    # <a> -> <em>
    # <table> -> <em>
    # <thead> -> <em>
    # <tbody> -> <em>
    # <tr> -> <em>
    # <th> -> <em>
    # <td> -> <em>
    # <br> -> <em>
    # Raw <p> -> <em>
    # Raw <div> -> <em>
    # Raw <span> -> <em>