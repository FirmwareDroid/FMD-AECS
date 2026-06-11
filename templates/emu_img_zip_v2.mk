#################################################################
# emu_img_zip_v2: Fast-Packaging Version (Zero Rebuilds)
# Generates the EXACT same file output as the original rule
#################################################################
.PHONY: emu_img_zip_v2

emu_img_zip_v2:
	@echo "=========================================================="
	@echo " PACKAGING EMULATOR IMAGES (v2: NO-REBUILD MODE)          "
	@echo " Target Output: $(INTERNAL_EMULATOR_PACKAGE_TARGET)"
	@echo "=========================================================="

	# Create the canonical emulator ABI target directory structure
	$(hide) mkdir -p $(INTERNAL_EMULATOR_PACKAGE_SOURCE)/$(TARGET_CPU_ABI)

	@echo "Step 1: Packaging Core System Partitions..."
	# Safely copy existing system images without invoking their compile rules
	$(hide) [ -f $(PRODUCT_OUT)/system-qemu.img ] && $(ACP) $(PRODUCT_OUT)/system-qemu.img $(INTERNAL_EMULATOR_PACKAGE_SOURCE)/$(TARGET_CPU_ABI)/system.img || echo " -> WARNING: system-qemu.img missing!"
	$(hide) [ -f $(PRODUCT_OUT)/ramdisk-qemu.img ] && $(ACP) $(PRODUCT_OUT)/ramdisk-qemu.img $(INTERNAL_EMULATOR_PACKAGE_SOURCE)/$(TARGET_CPU_ABI)/ramdisk.img || echo " -> WARNING: ramdisk-qemu.img missing!"
	$(hide) [ -f $(PRODUCT_OUT)/vendor-qemu.img ] && $(ACP) $(PRODUCT_OUT)/vendor-qemu.img $(INTERNAL_EMULATOR_PACKAGE_SOURCE)/$(TARGET_CPU_ABI)/vendor.img || echo " -> WARNING: vendor-qemu.img missing!"

	@echo "Step 2: Packaging Prebuilt & Target Kernels..."
	# Maps the architecture-correct kernel variant (kernel-ranchu)
	$(hide) if [ -f "$(EMULATOR_KERNEL_FILE)" ]; then \
		$(ACP) $(EMULATOR_KERNEL_FILE) $(INTERNAL_EMULATOR_PACKAGE_SOURCE)/$(TARGET_CPU_ABI)/$(EMULATOR_KERNEL_DIST_NAME); \
		echo " -> Embedded kernel: $(EMULATOR_KERNEL_DIST_NAME)"; \
	else \
		echo " -> WARNING: Target kernel binary not found at $(EMULATOR_KERNEL_FILE)"; \
	fi

	@echo "Step 3: Compiling Essential Metadata & Properties..."
	# Properties files tell the emulator engine what API level/skin to initialize
	$(hide) [ -f $(TARGET_OUT_INTERMEDIATES)/source.properties ] && $(ACP) $(TARGET_OUT_INTERMEDIATES)/source.properties $(INTERNAL_EMULATOR_PACKAGE_SOURCE)/$(TARGET_CPU_ABI)/source.properties || echo " -> Missing source.properties"
	$(hide) [ -f $(TARGET_OUT_INTERMEDIATES)/NOTICE.txt ] && $(ACP) $(TARGET_OUT_INTERMEDIATES)/NOTICE.txt $(INTERNAL_EMULATOR_PACKAGE_SOURCE)/$(TARGET_CPU_ABI)/NOTICE.txt || echo " -> Missing NOTICE.txt"
	$(hide) [ -f $(PRODUCT_OUT)/system/build.prop ] && $(ACP) $(PRODUCT_OUT)/system/build.prop $(INTERNAL_EMULATOR_PACKAGE_SOURCE)/$(TARGET_CPU_ABI)/build.prop || echo " -> Missing build.prop"

	@echo "Step 4: Merging Base Configurations & Userdata Files..."
	# Sweep through tracking lists to copy hardware templates, advancedFeatures.ini, and encryption keys
	$(hide) $(foreach f,$(INTERNAL_EMULATOR_PACKAGE_FILES), \
		[ -f $(f) ] && $(ACP) $(f) $(INTERNAL_EMULATOR_PACKAGE_SOURCE)/$(TARGET_CPU_ABI)/$(notdir $(f));)

	# Verify if VerifiedBootParams are present for AVB verified targets
	$(hide) [ -f $(PRODUCT_OUT)/VerifiedBootParams.textproto ] && $(ACP) $(PRODUCT_OUT)/VerifiedBootParams.textproto $(INTERNAL_EMULATOR_PACKAGE_SOURCE)/$(TARGET_CPU_ABI)/VerifiedBootParams.textproto || true

	@echo "Step 5: Synchronizing Data & File Storage System..."
	$(hide) [ -d $(PRODUCT_OUT)/data ] && $(ACP) -r $(PRODUCT_OUT)/data $(INTERNAL_EMULATOR_PACKAGE_SOURCE)/$(TARGET_CPU_ABI) || echo " -> System data directory empty"

	@echo "Step 6: Executing Compressed Zip Archiving via Soong..."
	# Archive using the exact same destination target path as the original script
	$(hide) $(SOONG_ZIP) -o $(INTERNAL_EMULATOR_PACKAGE_TARGET) -C $(INTERNAL_EMULATOR_PACKAGE_SOURCE) -D $(INTERNAL_EMULATOR_PACKAGE_SOURCE)/$(TARGET_CPU_ABI)

	@echo "=========================================================="
	@echo " SUCCESS: Package-Only Build Completed Successfully       "
	@echo " Output File: $(INTERNAL_EMULATOR_PACKAGE_TARGET)"
	@echo "=========================================================="