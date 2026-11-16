"""Instance-local adapter: SDK source and other mods' providers stay unchanged."""
from unrealsdk.unreal import WeakPointer
from willow2_mod_menu.data_providers.mod_options import ModOptionsDataProvider
from willow2_mod_menu.data_providers import OPTION_EVENT_ID_OFFSET


class ManagerOptionsProvider(ModOptionsDataProvider):
    def configure_manager(self, native, the_list, show_page, populate_keys):
        self.native = WeakPointer(native)
        self.the_list = WeakPointer(the_list)
        self.movie = WeakPointer(the_list.MyOwnerMovie)
        self.show_page = show_page
        self.populate_manager_keys = populate_keys

    def populate(self, native, the_list):
        native.KeyBinds = []
        native.CurrentKeyBindSelection = -1
        native.bNeedsToSaveKeyBinds = False
        self.drawn_keybinds.clear()
        super().populate(native, the_list)

    def category(self, event_id):
        index = int(event_id) - OPTION_EVENT_ID_OFFSET
        if 0 <= index < len(self.drawn_options):
            return getattr(self.drawn_options[index], '_cm_category', None)
        return None

    def handle_click(self, event_id, the_list):
        category = self.category(event_id)
        if category is not None:
            # Controller A/Right are handled before Flash dispatch. A mouse
            # category click previews it, without jumping into its list.
            self.show_page(the_list.MyOwnerMovie, category)
            return True
        return super().handle_click(event_id, the_list)

    def populate_keybind_keys(self, native):
        self.populate_manager_keys(native)

    def handle_key_rebind(self, native, key):
        return None  # Our dialogs persist controller intent, never keyboard binds.

    def handle_reset_keybinds(self):
        return None


def adapt_provider(provider, native, the_list, show_page, populate_keys):
    if not isinstance(provider, ManagerOptionsProvider):
        # The installed dataclasses have no slots. Keep the already generated
        # left options and SDK bookkeeping without changing global classes.
        provider.__class__ = ManagerOptionsProvider
    provider.configure_manager(native, the_list, show_page, populate_keys)
    return provider
