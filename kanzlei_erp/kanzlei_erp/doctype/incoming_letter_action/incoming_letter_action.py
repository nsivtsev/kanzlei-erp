from kanzlei_erp.posteingang_model import ProtectedRecord


class IncomingLetterAction(ProtectedRecord):
	def get_valid_dict(self, *args, **kwargs):
		data = super().get_valid_dict(*args, **kwargs)
		# Frappe's generic Int serialization casts None to zero. A manual
		# action has no source index; zero belongs to the first AI proposal.
		if not self.source_run:
			data["source_index"] = None
		return data
